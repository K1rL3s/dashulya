import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import httpx2
import pytest
from ag2 import MemoryStream
from ag2.config.openai.mappers import events_to_responses_input
from ag2.context import ConversationContext
from ag2.events import (
    ModelRequest,
    ModelResponse,
    TextInput,
    ToolCallEvent,
    ToolErrorEvent,
    ToolResult,
    ToolResultEvent,
    ToolResultsEvent,
    Usage,
)
from dirty_equals import IsInt, IsPartialDict
from openai import BadRequestError

from dashulya.ai.agents.tracing import ReplyTrace
from dashulya.domain.memory import MemoryAccess, MemoryInputError
from dashulya.observability import JsonLogFormatter


@pytest.mark.usefixtures("_setup")
class TestAgentTracing:
    @pytest.fixture
    def _setup(self) -> None:
        self.context = ConversationContext(
            MemoryStream(),
            dependencies={
                "memory_access": MemoryAccess(
                    -123, False, before=datetime(2026, 10, 27, tzinfo=UTC), before_id=7
                )
            },
        )
        self.event = ModelRequest(parts=[TextInput("PRIVATE REQUEST")])
        self.trace = ReplyTrace(self.event, self.context)

    async def test_model_timing_and_usage_are_logged_without_payload(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        response = ModelResponse(
            usage=Usage(prompt_tokens=100, completion_tokens=25, thinking_tokens=15),
            model="test-model",
        )
        call = AsyncMock(return_value=response)
        caplog.clear()
        with (
            caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"),
            patch("dashulya.ai.agents.tracing.perf_counter", side_effect=[10, 10.125]),
        ):
            result = await self.trace.on_llm_call(call, [self.event], self.context)
        assert any(
            record.name == "dashulya.ai.agents.tracing"
            or record.name.startswith("dashulya.ai.agents.tracing.")
            for record in caplog.records
        )
        assert result is response
        event = json.loads(JsonLogFormatter().format(caplog.records[-1]))
        assert event == IsPartialDict(
            level="INFO",
            duration_ms=125,
            prompt_tokens=100,
            thinking_tokens=15,
            message_id=7,
            outcome="ok",
        )
        assert "PRIVATE REQUEST" not in str(event)

    async def test_success_and_tool_error_keep_same_call_id_without_arguments_or_result(
        self,
        caplog: pytest.LogCaptureFixture,
        subtests: pytest.Subtests,
    ) -> None:
        call = ToolCallEvent(
            "search_memory", id="call-1", arguments='{"query":"PRIVATE QUERY"}'
        )
        for result, outcome in (
            (ToolResultEvent.from_call(call, "PRIVATE RESULT"), "ok"),
            (
                ToolErrorEvent.from_call(call, ValueError("PRIVATE PROVIDER ERROR")),
                "error",
            ),
        ):
            caplog.clear()
            with (
                subtests.test(outcome=outcome),
                caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"),
            ):
                returned = await self.trace.on_tool_execution(
                    AsyncMock(return_value=result), call, self.context
                )
                if outcome == "ok":
                    assert returned is result
                else:
                    assert isinstance(returned, ToolErrorEvent)
                    assert isinstance(result, ToolErrorEvent)
                    assert returned.parent_id == call.id
                    assert returned.error is result.error
                    assert "ValueError" in str(returned.result)
                    assert "PRIVATE" not in str(returned.result)
            assert any(
                record.name == "dashulya.ai.agents.tracing"
                or record.name.startswith("dashulya.ai.agents.tracing.")
                for record in caplog.records
            )
            events = [
                json.loads(JsonLogFormatter().format(record)) for record in caplog.records
            ]
            assert events == [
                IsPartialDict(tool_call_id="call-1", chat_id=-123),
                IsPartialDict(
                    tool_call_id="call-1",
                    chat_id=-123,
                    outcome=outcome,
                    level="ERROR" if outcome == "error" else "INFO",
                ),
            ]
            assert "PRIVATE" not in json.dumps(events)

    async def test_tool_exception_and_cancellation_are_traced_and_propagated(
        self,
        caplog: pytest.LogCaptureFixture,
        subtests: pytest.Subtests,
    ) -> None:
        for error, outcome in (
            (RuntimeError("PRIVATE ERROR"), "error"),
            (asyncio.CancelledError(), "cancelled"),
        ):
            caplog.clear()
            with (
                subtests.test(outcome=outcome),
                caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"),
                pytest.raises(type(error)),
            ):
                await self.trace.on_tool_execution(
                    AsyncMock(side_effect=error),
                    ToolCallEvent("other_tool"),
                    self.context,
                )
            assert any(
                record.name == "dashulya.ai.agents.tracing"
                or record.name.startswith("dashulya.ai.agents.tracing.")
                for record in caplog.records
            )
            event = json.loads(JsonLogFormatter().format(caplog.records[-1]))
            assert event == IsPartialDict(
                outcome=outcome,
                duration_ms=IsInt(ge=0),
                level="ERROR" if outcome == "error" else "INFO",
            )
            assert "PRIVATE ERROR" not in str(event)

    async def test_model_failure_and_cancellation_are_logged_and_propagated(
        self, caplog: pytest.LogCaptureFixture, subtests: pytest.Subtests
    ) -> None:
        for error, outcome, level in (
            (RuntimeError("PRIVATE ERROR"), "error", "ERROR"),
            (asyncio.CancelledError(), "cancelled", "INFO"),
        ):
            caplog.clear()
            with (
                subtests.test(outcome=outcome),
                caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"),
                pytest.raises(type(error)) as raised,
            ):
                await self.trace.on_llm_call(
                    AsyncMock(side_effect=error), [self.event], self.context
                )
            assert raised.value is error
            event = json.loads(JsonLogFormatter().format(caplog.records[-1]))
            assert event == IsPartialDict(
                message="AI model call completed",
                chat_id=-123,
                message_id=7,
                outcome=outcome,
                duration_ms=IsInt(ge=0),
                level=level,
            )
            if outcome == "error":
                assert event["exceptions"][0]["type"] == "RuntimeError"
                assert event["exceptions"][0]["frames"]
            assert "PRIVATE" not in str(event)

    async def test_memory_failures_log_original_trace_chain_and_return_a_plain_string(
        self, caplog: pytest.LogCaptureFixture, subtests: pytest.Subtests
    ) -> None:
        async def fail() -> None:
            try:
                private_message = "PRIVATE CAUSE"
                raise RuntimeError(private_message)
            except RuntimeError as cause:
                error_message = "History dates must include a timezone."
                raise MemoryInputError(error_message) from cause

        with pytest.raises(MemoryInputError) as raised:
            await fail()
        original = raised.value
        call = ToolCallEvent("chat_history", id="failed-call")
        for throws in (False, True):
            caplog.clear()
            downstream = (
                AsyncMock(side_effect=original)
                if throws
                else AsyncMock(return_value=ToolErrorEvent.from_call(call, original))
            )
            with (
                subtests.test(throws=throws),
                caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"),
            ):
                if throws:
                    with pytest.raises(MemoryInputError) as propagated:
                        await self.trace.on_tool_execution(downstream, call, self.context)
                    assert propagated.value is original
                else:
                    result = await self.trace.on_tool_execution(
                        downstream, call, self.context
                    )
                    assert isinstance(result, ToolErrorEvent)
                    assert result.name == "chat_history"
                    assert result.parent_id == "failed-call"
                    assert result.result == ToolResult(
                        "chat_history failed (MemoryInputError): History dates must include a "
                        "timezone. Correct the arguments within the current memory access scope."
                    )
                    assert "Traceback" not in repr(result)
                    assert "PRIVATE CAUSE" not in repr(result)
            event = json.loads(JsonLogFormatter().format(caplog.records[-1]))
            assert event == IsPartialDict(
                level="WARNING",
                tool_call_id="failed-call",
                chat_id=-123,
                message_id=7,
                outcome="error",
                error_type="MemoryInputError",
                error_message="History dates must include a timezone.",
            )
            assert [item["type"] for item in event["exceptions"]] == [
                "MemoryInputError",
                "RuntimeError",
            ]
            assert all(item["frames"] for item in event["exceptions"])
            assert event["exceptions"][0]["frames"][-1]["function"] == "fail"
            assert "PRIVATE CAUSE" not in str(event)

    async def test_memory_cancellation_propagates_without_a_synthetic_error(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await self.trace.on_tool_execution(
                AsyncMock(side_effect=asyncio.CancelledError()),
                ToolCallEvent("search_memory"),
                self.context,
            )

    async def test_memory_provider_error_keeps_diagnostics_out_of_model_history(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def fail() -> None:
            private_message = "PRIVATE PROVIDER BODY"
            raise BadRequestError(
                private_message,
                response=httpx2.Response(
                    400,
                    request=httpx2.Request("POST", "https://example.com/embeddings"),
                    headers={"x-request-id": "req-test"},
                ),
                body={"code": "invalid_value", "param": "input"},
            )

        call = ToolCallEvent("search_memory", id="provider-failure")
        with pytest.raises(BadRequestError) as raised:
            await fail()
        failure = ToolErrorEvent.from_call(call, raised.value)
        with caplog.at_level("INFO", logger="dashulya.ai.agents.tracing"):
            result = await self.trace.on_tool_execution(
                AsyncMock(return_value=failure),
                call,
                self.context,
            )
        assert isinstance(result, ToolErrorEvent)
        (part,) = result.result.parts
        assert isinstance(part, TextInput)
        assert "BadRequestError" in part.content
        assert "Do not treat this as an empty result" in part.content
        assert "Use chat_history" in part.content
        assert "Traceback" not in part.content
        wire_input = events_to_responses_input([ToolResultsEvent([result])], Mock())
        assert wire_input == [
            {
                "type": "function_call_output",
                "call_id": "provider-failure",
                "output": part.content,
            }
        ]
        assert "PRIVATE" not in str(wire_input)
        event = json.loads(JsonLogFormatter().format(caplog.records[-1]))
        assert event == IsPartialDict(
            level="ERROR",
            status_code=400,
            error_code="invalid_value",
            error_param="input",
            request_id="req-test",
        )
        assert event["exceptions"][0]["frames"][-1]["function"] == "fail"
        assert "PRIVATE" not in str(event)
