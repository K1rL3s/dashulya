"""Measure bounded retrieval and analytical replies on an isolated runtime copy."""

# ruff: file-ignore[ambiguous-unicode-character-string] -- Russian evaluation queries.

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from ag2 import Context
from ag2.config import OpenAIResponsesConfig
from ag2.events import ModelResponse, ToolCallEvent, ToolCallsEvent
from ag2.middleware import Middleware
from ag2.testing import TestConfig
from dishka import Provider, from_context, make_async_container, provide
from dishka_ag2 import AG2Provider, AG2Scope, DishkaAsyncMiddleware
from dotenv import load_dotenv
from openai import AsyncOpenAI
from psycopg.conninfo import conninfo_to_dict

from dashulya.ai.agents.factory import create_reply_agent
from dashulya.ai.dialogue import Dialogue
from dashulya.ai.embeddings import MemoryEmbeddings
from dashulya.ai.message_format import MessageFormatter
from dashulya.ai.tools.history import chat_history
from dashulya.ai.tools.memory import search_memory
from dashulya.database import Database
from dashulya.domain.memory import (
    ChatMemoryService,
    HistoryReader,
    MemoryAccess,
    MemoryBackend,
)
from dashulya.domain.messages import ReplyRequest
from dashulya.memory.service import HybridChatMemory
from dashulya.memory.store import MessageStore
from dashulya.memory.vectors import EmbeddingCache

QUERIES = (
    "Как участники обсуждали работу с контекстом и памятью AI-агентов?",
    "Какие решения предлагали для ускорения поиска по сообщениям?",
    "Опыт использования Claude Code и Codex для разработки",
    "Ошибки и ограничения PostgreSQL и векторного поиска",
    "Обсуждение Python Django и типизации",
    "Сравнение моделей и стоимости использования LLM",
    "Настройка MCP инструментов для агентов",
    "Как оценивают качество ответов и тестируют RAG",
)


class _Tools(Provider):
    scope = AG2Scope.APP
    service = from_context(ChatMemoryService)
    formatter = from_context(MessageFormatter)

    @provide(scope=AG2Scope.SESSION)
    def access(self, context: Context) -> MemoryAccess:  # ruff: ignore[no-self-use] -- Evaluation provider.
        value = context.dependencies["memory_access"]
        if not isinstance(value, MemoryAccess):
            message = "A trusted evaluation access scope is required."
            raise TypeError(message)
        return value

    @provide(scope=AG2Scope.SESSION)
    def recall(self, service: ChatMemoryService, access: MemoryAccess) -> MemoryBackend:  # ruff: ignore[no-self-use] -- Evaluation provider.
        return service.recall(access)

    @provide(scope=AG2Scope.SESSION)
    def history(self, service: ChatMemoryService, access: MemoryAccess) -> HistoryReader:  # ruff: ignore[no-self-use] -- Evaluation provider.
        return service.history(access)


class _Timings(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() in {
            "Memory search completed",
            "AI tool call completed",
            "AI model call completed",
        }:
            self.rows.append({
                "message": record.getMessage(),
                **{
                    key: value
                    for key, value in vars(record).items()
                    if key in {"duration_ms", "tool_name", "outcome", "search_metrics"}
                },
            })


async def _analytical(
    memory: HybridChatMemory,
    store: MessageStore,
    timings: _Timings,
    output: Path,
    chat_id: int,
    *,
    scripted: bool = False,
    parallel: int = 1,
    query_suffix: str = "",
) -> list[dict[str, Any]]:
    container = make_async_container(
        _Tools(),
        AG2Provider(),
        scopes=AG2Scope,
        context={
            ChatMemoryService: memory,
            MessageFormatter: MessageFormatter("Europe/Moscow"),
        },
        lock_factory=asyncio.Lock,
    )
    config = (
        TestConfig()
        if scripted
        else OpenAIResponsesConfig(
            model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
            api_key=os.environ["OPENAI_API_KEY"],
            reasoning={"effort": "low"},
        )
    )
    agent = create_reply_agent(
        config,
        tools=[search_memory, chat_history],
        middleware=[Middleware(DishkaAsyncMiddleware, container=container)],
    )
    dialogue = Dialogue(agent, history=store)
    rows = []
    try:
        for number, base_question in enumerate(QUERIES if scripted else QUERIES[:3], 1):
            question = base_question + query_suffix
            if isinstance(config, TestConfig):
                config.events = (
                    ModelResponse(
                        tool_calls=ToolCallsEvent([
                            ToolCallEvent(
                                "search_memory",
                                arguments=json.dumps({
                                    "query": question
                                    + f" С фактами из обсуждения, вариант {index}."
                                }),
                            )
                            for index in range(parallel)
                        ])
                    ),
                    json.dumps({
                        "messages": [
                            {
                                "text": "Scripted benchmark completed.",
                                "reply_to_message_id": None,
                            }
                        ]
                    }),
                )
            timings.rows.clear()
            start = perf_counter()
            reply = await dialogue.reply(
                ReplyRequest(
                    (chat_id, None),
                    question
                    + " Сопоставь мнения участников и приведи ссылки на сообщения.",
                    user_id=42,
                    user_name="Evaluation owner",
                    sent_at=datetime.now(UTC),
                    message_id=1_000_000_000 + number,
                )
            )
            row = {
                "case": number,
                "model_mode": "scripted" if scripted else "real",
                "parallel_tools": parallel if scripted else None,
                "duration_ms": round((perf_counter() - start) * 1000),
                "events": list(timings.rows),
            }
            (output / f"answer-{number}.json").write_text(
                json.dumps(
                    [message.text for message in reply.messages],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            rows.append(row)
            sys.stdout.write(
                json.dumps({
                    "analytical_case": number,
                    "duration_ms": row["duration_ms"],
                })
                + "\n",
            )
            sys.stdout.flush()
    finally:
        await container.close()
    return rows


async def run(args: argparse.Namespace) -> None:
    load_dotenv()
    url = os.environ.get("EVAL_DATABASE_URL", "")
    if not str(conninfo_to_dict(url).get("dbname", "")).startswith(
        "dashulya_search_speed_"
    ):
        message = "Use a dedicated dashulya_search_speed_ database containing an authorized runtime copy."
        raise ValueError(message)
    args.output.mkdir(parents=True, exist_ok=True)
    database = Database(url)
    timings = _Timings()
    logger = logging.getLogger("dashulya")
    logger.setLevel(logging.INFO)
    logger.addHandler(timings)
    async with AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"], timeout=15, max_retries=0
    ) as client:
        await database.open()
        store = MessageStore(database)
        memory = HybridChatMemory(
            store,
            EmbeddingCache(database, MemoryEmbeddings(client)),
            owner_user_id=42,
        )
        try:
            prepared = perf_counter()
            await memory.prepare_cached()
            (args.output / "preparation.json").write_text(
                json.dumps({
                    "duration_ms": round((perf_counter() - prepared) * 1000),
                    "historical_embedding_calls": 0,
                })
            )
            recall = memory.recall(
                MemoryAccess(args.chat_id, False, before=datetime.now(UTC))
            )
            rows = []
            for repeat in range(args.repeat):
                for number, base_question in enumerate(QUERIES, 1):
                    question = base_question + args.query_suffix
                    timings.rows.clear()
                    started = perf_counter()
                    result = await recall.search(question)
                    row = {
                        "case": number,
                        "repeat": repeat,
                        "duration_ms": round((perf_counter() - started) * 1000),
                        "hits": len(result["hits"]),
                        "coverage": result["coverage"],
                        "events": list(timings.rows),
                    }
                    rows.append(row)
                    sys.stdout.write(
                        json.dumps({
                            key: row[key]
                            for key in ("case", "repeat", "duration_ms", "hits")
                        })
                        + "\n"
                    )
                    sys.stdout.flush()
                    (args.output / "retrieval.json").write_text(
                        json.dumps(rows, ensure_ascii=False, indent=2)
                    )
            if args.agent or args.scripted:
                answers = await _analytical(
                    memory,
                    store,
                    timings,
                    args.output,
                    args.chat_id,
                    scripted=args.scripted,
                    parallel=args.parallel,
                    query_suffix=args.query_suffix,
                )
                (args.output / "analytical.json").write_text(
                    json.dumps(answers, ensure_ascii=False, indent=2)
                )
        finally:
            await memory.close()
            await database.close()
            logger.removeHandler(timings)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--repeat", type=int, default=4)
    parser.add_argument("--agent", action="store_true")
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="Use a local scripted model; history stays local.",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Concurrent memory tools per scripted analytical turn.",
    )
    parser.add_argument("--query-suffix", default="")
    parser.add_argument(
        "--output", type=Path, default=Path("evals/runs/search-3s-20261101")
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
