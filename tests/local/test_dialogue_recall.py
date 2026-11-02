import asyncio
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx2
import pytest
from ag2 import Agent
from ag2.config import ModelConfig, OpenAIResponsesConfig
from ag2.events import ToolCallEvent
from ag2.testing import TestConfig
from dirty_equals import IsPartialDict

from dashulya.ai.dialogue import Dialogue
from dashulya.domain.memory import (
    ChatMemoryService,
    HistoryReader,
    MemoryAccess,
    MemoryBackend,
)
from dashulya.domain.messages import Reply, ReplyBatch, ReplyRequest
from dashulya.main import create_container
from dashulya.main.config import Settings
from tests.support import (
    TEST_API_KEY,
    MemoryOverride,
    ModelOverrides,
    input_payload,
    reply_json,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.mark.usefixtures("_setup")
class TestDialogueRecall:
    @pytest.fixture
    def _setup(self, cleanup: AsyncExitStack) -> None:
        self.cleanup = cleanup

    async def dialogue(
        self, *responses: str | ToolCallEvent, config: ModelConfig | None = None
    ) -> tuple[Dialogue, Agent[ReplyBatch], ChatMemoryService]:
        self.backend = AsyncMock(spec=MemoryBackend)
        self.backend.search.return_value = {"coverage": {}, "hits": []}
        self.reader = AsyncMock(spec=HistoryReader)
        self.reader.sql.return_value = {
            "query_count": 1,
            "results": [{"count": 1, "coverage": {}}],
        }
        container = create_container(
            Settings(bot_token="123:TEST", openai_api_key=TEST_API_KEY, owner_user_id=42),
            MemoryOverride(self.backend, reader=self.reader),
            ModelOverrides(
                reply=config or TestConfig(*responses), decision=TestConfig("false")
            ),
        )
        self.cleanup.push_async_callback(container.close)
        return (
            await container.get(Dialogue),
            await container.get(Agent[ReplyBatch]),
            await container.get(ChatMemoryService),
        )

    @staticmethod
    async def reply(dialogue: Dialogue, *, chat_id: int = 42) -> ReplyBatch:
        return await dialogue.reply(
            ReplyRequest(
                (chat_id, None),
                "Current request",
                user_id=42,
                user_name="Owner",
                sent_at=NOW,
                message_id=2,
            )
        )

    async def test_private_and_group_followups_do_not_read_memory_without_tool_calls(
        self,
    ) -> None:
        dialogue, _, service = await self.dialogue(
            *(reply_json("Answer") for _ in range(4))
        )
        with patch.object(service, "recall", wraps=service.recall) as recall:
            for chat_id in (42, -123):
                for _ in range(2):
                    assert await self.reply(dialogue, chat_id=chat_id) == ReplyBatch((
                        Reply("Answer"),
                    ))
        recall.assert_not_called()
        self.backend.search.assert_not_awaited()
        self.reader.sql.assert_not_awaited()

    async def test_model_selected_memory_and_history_share_current_trusted_scope(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dialogue, agent, service = await self.dialogue(
            ToolCallEvent(
                "search_memory", arguments='{"query":"Что мы решили про RTK?"}'
            ),
            ToolCallEvent(
                "chat_history",
                arguments='{"sql": ["SELECT count(*) AS count FROM messages"]}',
            ),
            reply_json("Answer", 999),
        )
        caplog.clear()
        with (
            patch.object(service, "recall", wraps=service.recall) as recall,
            patch.object(service, "history", wraps=service.history) as history,
            patch.object(agent, "ask", wraps=agent.ask) as ask,
            caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"),
        ):
            assert await self.reply(dialogue, chat_id=-123) == ReplyBatch((
                Reply("Answer"),
            ))
        assert any(
            record.name == "dashulya.ai.agents.tracing"
            or record.name.startswith("dashulya.ai.agents.tracing.")
            for record in caplog.records
        )
        access = ask.call_args.kwargs["dependencies"]["memory_access"]
        assert access == MemoryAccess(-123, False, before=NOW, before_id=2)
        recall.assert_called_once_with(access)
        history.assert_called_once_with(access)
        assert self.backend.search.await_count == 1
        assert self.backend.search.await_args.args[0] == "Что мы решили про RTK?"
        assert self.reader.sql.await_count == 1
        tools = [
            vars(record)
            for record in caplog.records
            if record.message == "AI tool call completed"
        ]
        assert tools == [
            IsPartialDict(tool_name=name, outcome="ok", chat_id=-123, message_id=2)
            for name in ("search_memory", "chat_history")
        ]

    async def test_history_only_request_does_not_invoke_semantic_memory(self) -> None:
        dialogue, _, service = await self.dialogue(
            ToolCallEvent(
                "chat_history",
                arguments='{"sql": ["SELECT count(*) AS count FROM messages"]}',
            ),
            reply_json("Counted"),
        )
        with patch.object(
            service, "recall", side_effect=AssertionError("Recall was not requested")
        ):
            assert (await self.reply(dialogue)).messages[0].text == "Counted"
        self.reader.sql.assert_awaited_once()
        self.backend.search.assert_not_awaited()

    async def test_turn_deadline_cancels_a_selected_memory_tool(self) -> None:
        dialogue, _, _ = await self.dialogue(
            ToolCallEvent("search_memory", arguments='{"query":"Earlier discussion"}'),
            reply_json("Unreachable"),
        )
        cancelled = asyncio.Event()

        async def stalled(query: str, **kwargs: object) -> dict[str, object]:
            try:
                await asyncio.Future[None]()
            finally:
                cancelled.set()
            return {}

        self.backend.search.side_effect = stalled
        timeout = asyncio.timeout
        with (
            patch(
                "dashulya.ai.dialogue.asyncio.timeout",
                side_effect=lambda seconds: timeout(0.1),
            ),
            pytest.raises(TimeoutError),
        ):
            await self.reply(dialogue)
        assert cancelled.is_set()

    @pytest.mark.parametrize("partial_batch", [False, True])
    async def test_next_request_repairs_interrupted_tools_and_preserves_history(
        self, partial_batch: bool
    ) -> None:
        requests: list[dict[str, Any]] = []

        def respond(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 2:
                output: list[dict[str, Any]] = [
                    {
                        "type": "function_call",
                        "call_id": "interrupted",
                        "name": "search_memory",
                        "arguments": '{"query":"Earlier discussion"}',
                    }
                ]
                if partial_batch:
                    output.append({
                        "type": "function_call",
                        "call_id": "completed",
                        "name": "chat_history",
                        "arguments": '{"sql":["SELECT count(*) AS count FROM messages"]}',
                    })
            else:
                if len(requests) >= 3:
                    items = body["input"]
                    results = {
                        item["call_id"]: item["output"]
                        for item in items
                        if item.get("type") == "function_call_output"
                    }
                    assert "interrupted" in results, "Unanswered tool reaches provider"
                    assert "not completed" in results["interrupted"]
                    if partial_batch:
                        assert json.loads(input_payload(results["completed"])) == {
                            "query_count": 1,
                            "results": [{"count": 1, "coverage": {}}],
                        }
                    assert any(
                        item.get("role") == "assistant"
                        and "Earlier answer" in item["content"]
                        for item in items
                    ), "Successful history was lost"
                    assert len(results) == (2 if partial_batch else 1)
                    assert sum(
                        item.get("type") == "function_call_output" for item in items
                    ) == len(results), "Repair duplicated tool results"
                output = [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": reply_json("Earlier answer"),
                                "annotations": [],
                            }
                        ],
                    }
                ]
            return httpx2.Response(
                200,
                json={
                    "id": f"resp_{len(requests)}",
                    "object": "response",
                    "created_at": 1700000000,
                    "status": "completed",
                    "model": "test-model",
                    "output": output,
                },
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            dialogue, _, _ = await self.dialogue(
                config=OpenAIResponsesConfig(
                    model="test-model", api_key=TEST_API_KEY, http_client=http
                )
            )
            await self.reply(dialogue)

            async def stalled(query: str, **kwargs: object) -> dict[str, object]:
                await asyncio.Future[None]()
                return {}

            self.backend.search.side_effect = stalled
            timeout = asyncio.timeout
            with (
                patch(
                    "dashulya.ai.dialogue.asyncio.timeout",
                    side_effect=lambda seconds: timeout(0.1),
                ),
                pytest.raises(TimeoutError),
            ):
                await self.reply(dialogue)
            for _ in range(2):
                assert await self.reply(dialogue) == ReplyBatch((
                    Reply("Earlier answer"),
                ))
        assert len(requests) == 4
        self.backend.search.assert_awaited_once()
