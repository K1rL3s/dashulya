"""Exercise the replay boundary with real PostgreSQL and scripted model turns."""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ag2.events import ModelMessage, ModelResponse, ToolCallEvent, ToolCallsEvent, Usage
from ag2.testing import TestConfig

from dashulya.database import Database
from dashulya.domain.memory import MemoryMessage
from dashulya.memory.service import HybridChatMemory
from dashulya.memory.store import MessageStore
from dashulya.memory.vectors import EmbeddingCache
from evals.suites.memory.diagnostics import DiagnosticLogs
from evals.suites.memory.runtime import isolated_database
from evals.suites.memory.scenario import Scenario, replay
from evals.suites.memory.snapshot import copy_snapshot
from tests.postgres import require_database_url
from tests.support import reply_json


async def embed(texts: list[str]) -> list[list[float]]:
    await asyncio.sleep(0.02)
    return [[1.0] + [0.0] * 511 for _ in texts]


async def test_replay_records_calls_context_timings_and_scope(
    database: Database, database_url: str
) -> None:
    sent = datetime(2026, 1, 1, 10, tzinfo=UTC)
    store = MessageStore(database)
    parent = MemoryMessage(-1001, 1, "Aurora proposal", sent, 42, "Alice", "Group")
    child = replace(
        parent, message_id=2, text="Aurora decision", author_id=20, reply_to_id=1
    )
    for message in (
        parent,
        child,
        replace(parent, chat_id=-1002, text="FOREIGN SECRET"),
        replace(
            parent, message_id=9, text="FUTURE SECRET", sent_at=sent + timedelta(days=3)
        ),
    ):
        await store.record(message)
    memory = HybridChatMemory(store, EmbeddingCache(database, embed), owner_user_id=42)
    await memory.refresh_once()
    await memory.close()
    case = Scenario(
        case_id="summary",
        session="group",
        query="What did Alice decide about Aurora?",
        chat_id=-1001,
        target_chat_id=-1001,
        user_id=42,
        sent_at=sent + timedelta(days=1),
        after=sent,
        before=sent + timedelta(hours=1),
        author_id=20,
    )
    searches = [
        ToolCallEvent(
            "search_memory",
            id=f"recall-{i}",
            arguments='{"query":"Aurora '
            + str(i)
            + '","author_id":20,"after":"2026-01-01T10:00:00+00:00","before":"2026-01-01T11:00:00+00:00"}',
        )
        for i in range(2)
    ]
    history = ToolCallEvent(
        "chat_history",
        id="history",
        arguments='{"sql":["SELECT source_id,message_id,chat_id,text,url FROM messages WHERE author_id=20 ORDER BY message_id LIMIT 50","SELECT count(*) AS total FROM messages"]}',
    )
    config = TestConfig(
        ModelResponse(
            tool_calls=ToolCallsEvent([*searches, history]),
            usage=Usage(prompt_tokens=100),
        ),
        ModelResponse(
            ModelMessage(reply_json("[Aurora decision](https://t.me/c/1/2)")),
            usage=Usage(prompt_tokens=300),
        ),
    )
    handler = DiagnosticLogs()
    logger = logging.getLogger("dashulya")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        async with isolated_database(require_database_url()) as target:
            snapshot = await copy_snapshot(database_url, target, before=case.sent_at)
            assert snapshot["counts"]["messages"] == 3
            assert snapshot["counts"]["embeddings"] >= 4
            reports = await replay(target, [case], config, embed, bot_id=999)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        handler.close()
    (report,) = reports
    assert report["error"] is None
    assert report["evaluation"]["passed"], report["evaluation"]
    assert len(report["models"]) == 2
    assert len(report["tools"]) == 3
    assert len(report["embeddings"]) == 2
    assert report["timing"]["tool_wall_ms"] < sum(
        t["duration_ms"] for t in report["tools"]
    )
    assert "FOREIGN SECRET" not in str(report["models"])
    assert "FUTURE SECRET" not in str(report["models"])
    assert "Aurora decision" in str(report["models"][-1]["events"])
    assert report["models"][0]["tools"]
    stages = [e for e in report["logs"] if e["message"] == "Memory search completed"]
    assert len(stages) == 2
    assert {e["tool_call_id"] for e in stages} == {"recall-0", "recall-1"}
    assert all(e["search_metrics"]["candidate_count"] == 1 for e in stages)
    sql = [e for e in report["logs"] if e["message"] == "History SQL query completed"]
    assert len(sql) == 2
    assert all(e["tool_call_id"] == "history" for e in sql)
    assert report["evaluation"]["period_originals"] == 1
    assert report["evaluation"]["checks"]["context_token_budget"]
