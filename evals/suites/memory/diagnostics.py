"""Observe real agent calls without changing their inputs or tool results."""

import json
import logging
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, override

from ag2 import Context
from ag2.events import (
    BaseEvent,
    ModelResponse,
    ToolCallEvent,
    ToolErrorEvent,
    ToolResultEvent,
)
from ag2.middleware.base import BaseMiddleware, LLMCall, ToolExecution, ToolResultType

from dashulya.observability import JsonLogFormatter

active_tool: ContextVar[str | None] = ContextVar("eval_active_tool", default=None)


def event_data(event: BaseEvent) -> dict[str, Any]:
    if isinstance(event, ToolErrorEvent):
        return {
            "type": "ToolErrorEvent",
            "name": event.name,
            "parent_id": event.parent_id,
            "error_type": type(event.error).__name__,
        }
    return event.to_dict()


@dataclass
class Diagnostics:
    models: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    embeddings: list[dict[str, Any]] = field(default_factory=list)
    results: list[ToolResultEvent] = field(default_factory=list, repr=False)
    initial_sources: set[str] = field(default_factory=set)
    context_ms: int = 0
    phases: dict[str, int] = field(default_factory=dict)
    request_message_id: int | None = None
    max_model_calls: int = 12
    max_tool_calls: int = 24
    started: float = field(default_factory=perf_counter)

    def offset(self) -> int:
        return round((perf_counter() - self.started) * 1000)


active_diagnostics: ContextVar[Diagnostics | None] = ContextVar(
    "eval_diagnostics", default=None
)


class DiagnosticLogs(logging.Handler):
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self.setFormatter(JsonLogFormatter(secrets))

    @override
    def emit(self, record: logging.LogRecord) -> None:
        if (diagnostics := active_diagnostics.get()) is not None:
            value = json.loads(self.format(record))
            value.update(offset_ms=diagnostics.offset())
            if active_tool.get() is not None:
                value.setdefault("tool_call_id", active_tool.get())
            diagnostics.logs.append(value)


class DiagnosticTrace(BaseMiddleware):
    @override
    async def on_llm_call(
        self, call_next: LLMCall, events: Sequence[BaseEvent], context: Context
    ) -> ModelResponse:
        diagnostics = active_diagnostics.get()
        if diagnostics is None:
            return await call_next(events, context)
        if len(diagnostics.models) >= diagnostics.max_model_calls:
            error_message = "Evaluation model-call budget exceeded."
            raise RuntimeError(error_message)
        item: dict[str, Any] = {
            "call_index": len(diagnostics.models) + 1,
            "start_ms": diagnostics.offset(),
            "prompt": list(context.prompt),
            "events": [event_data(event) for event in events],
            "event_count": len(events),
            "tool_result_events": sum(
                isinstance(event, ToolResultEvent) for event in events
            ),
        }
        item["input_chars"] = len(json.dumps(item["events"], ensure_ascii=False))
        diagnostics.models.append(item)
        started = perf_counter()
        try:
            response = await call_next(events, context)
            item.update(
                outcome="ok",
                response=event_data(response),
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                cached_input_tokens=response.usage.cache_read_input_tokens,
                thinking_tokens=response.usage.thinking_tokens,
            )
        except BaseException as error:
            item.update(outcome="error", error_type=type(error).__name__)
            raise
        finally:
            item["duration_ms"] = round((perf_counter() - started) * 1000)
        return response

    @override
    async def on_tool_execution(
        self, call_next: ToolExecution, event: ToolCallEvent, context: Context
    ) -> ToolResultType:
        diagnostics = active_diagnostics.get()
        if diagnostics is None:
            return await call_next(event, context)
        if len(diagnostics.tools) >= diagnostics.max_tool_calls:
            error_message = "Evaluation tool-call budget exceeded."
            raise RuntimeError(error_message)
        item: dict[str, Any] = {
            "tool_call_id": event.id,
            "name": event.name,
            "arguments": event.serialized_arguments,
            "start_ms": diagnostics.offset(),
        }
        diagnostics.tools.append(item)
        token = active_tool.set(event.id)
        started = perf_counter()
        try:
            result = await call_next(event, context)
            if isinstance(result, ToolResultEvent) and not isinstance(
                result, ToolErrorEvent
            ):
                diagnostics.results.append(result)
            item.update(
                outcome="error" if isinstance(result, ToolErrorEvent) else "ok",
                result=event_data(result),
            )
        except BaseException as error:
            item.update(outcome="error", error_type=type(error).__name__)
            raise
        finally:
            item["duration_ms"] = round((perf_counter() - started) * 1000)
            active_tool.reset(token)
        return result
