"""Request-correlated model output and tool timings, including history SQL."""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from contextlib import suppress
from time import perf_counter
from typing import Annotated, Any, override

from ag2 import Context, Inject, observer
from ag2.events import (
    BaseEvent,
    BinaryInput,
    BinaryType,
    BuiltinToolCallEvent,
    BuiltinToolResultEvent,
    ModelRequest,
    ModelResponse,
    TextInput,
    ToolCallEvent,
    ToolErrorEvent,
    ToolResult,
    ToolResultEvent,
)
from ag2.middleware.base import BaseMiddleware, LLMCall, ToolExecution, ToolResultType
from fast_depends import inject
from pydantic import ValidationError

from dashulya.domain.memory import MemoryAccess, MemoryInputError, message_source_id
from dashulya.domain.messages import ReplyRequest
from dashulya.observability import exception_fields, reply_log_fields

logger = logging.getLogger(__name__)


def _memory_failure(event: ToolCallEvent, error: Exception) -> ToolErrorEvent:
    message = f"{event.name} failed ({type(error).__name__}): "
    if isinstance(error, MemoryInputError):
        message += (
            f"{error} Correct the arguments within the current memory access scope."
        )
    elif isinstance(error, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['type']}"
            for item in error.errors(include_input=False, include_context=False)
        )
        message += f"Invalid arguments: {details}. Correct the arguments and try again."
    else:
        message += (
            "Memory lookup could not be completed. Do not treat this as an empty result "
            "or claim that no matching messages exist."
        )
        if event.name == "search_memory":
            message += " Use chat_history for canonical originals if available."
    return ToolErrorEvent(
        name=event.name,
        parent_id=event.id,
        error=error,
        result=ToolResult(message),
    )


def _request_fields(
    access: Annotated[MemoryAccess | None, Inject("memory_access", default=None)],
    request: Annotated[ReplyRequest | None, Inject("reply_request", default=None)],
) -> dict[str, object]:
    if isinstance(request, ReplyRequest) and request.is_whisper:
        return reply_log_fields(request)
    if isinstance(access, MemoryAccess):
        return {"chat_id": access.chat_id, "message_id": access.before_id}
    return {}


# AG2 calls middleware hooks directly, outside its annotation resolver.
_request: Callable[..., dict[str, object]] = inject(_request_fields, cast=False)


@observer(BuiltinToolCallEvent | BuiltinToolResultEvent)
async def log_builtin_tool(event: BaseEvent, context: Context) -> None:
    fields = _request(__ctx__=context)
    if isinstance(event, BuiltinToolCallEvent):
        fields["tool_name"] = event.name
        fields["tool_call_id"] = event.id
        logger.info("AI builtin tool call observed", extra=fields)
    elif isinstance(event, BuiltinToolResultEvent):
        fields["tool_name"] = event.name
        fields["tool_call_id"] = event.parent_id
        status = event.result.metadata.get("status")
        if isinstance(status, str):
            fields["tool_status"] = status
        logger.log(
            logging.ERROR if status == "failed" else logging.INFO,
            "AI builtin tool result observed",
            extra=fields,
        )


def _outcome_fields(
    started: float, fields: dict[str, object], outcome: str
) -> dict[str, object]:
    return {
        **fields,
        "duration_ms": round((perf_counter() - started) * 1000),
        "outcome": outcome,
    }


def _output_fields(response: ModelResponse) -> dict[str, object]:
    output = response.content
    count = None
    if output is not None:
        try:
            value = json.loads(output)
        except ValueError:
            pass
        else:
            if isinstance(value, dict) and isinstance(value.get("messages"), list):
                count = len(value["messages"])
    return {
        "agent_output": output,
        "message_count": count,
        "tool_call_count": len(response.tool_calls),
    }


def _history_sql_fields(event: ToolCallEvent) -> dict[str, object]:
    if event.name != "chat_history":
        return {}
    try:
        query = event.serialized_arguments.get("sql")
    except AttributeError, ValueError, TypeError:
        return {}
    if isinstance(query, list) and all(isinstance(item, str) for item in query):
        return {
            "sql": [item[:16_000] for item in query[:5]],
            "sql_truncated": len(query) > 5
            or any(len(item) > 16_000 for item in query[:5]),
            "query_count": len(query),
        }
    return {}


def _argument_fields(event: ToolCallEvent) -> dict[str, object]:
    if event.name == "chat_history":
        return _history_sql_fields(event)
    try:
        arguments = event.serialized_arguments
    except AttributeError, ValueError, TypeError:
        return {"tool_arguments_invalid": True}

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: "<REDACTED>"
                if re.search(
                    r"(?i)password|secret|token|api.?key|authorization|credential|headers",
                    key,
                )
                else redact(item)
                for key, item in value.items()
            }
        return [redact(item) for item in value] if isinstance(value, list) else value

    safe = redact(arguments)
    encoded = json.dumps(safe, ensure_ascii=False)
    return {
        "tool_arguments": safe if len(encoded) <= 16000 else encoded[:16000],
        "tool_arguments_truncated": len(encoded) > 16000,
    }


def _result_sources(value: Any, chat_id: int | None = None) -> set[str]:
    if isinstance(value, str):
        return {value} if re.fullmatch(r"source:[0-9a-f]{24}:[0-9]+", value) else set()
    if isinstance(value, list):
        return set().union(*(_result_sources(item, chat_id) for item in value))
    if not isinstance(value, dict) or value.get("status") == "not_found":
        return set()
    sources: set[str] = set()
    columns = value.get("columns")
    if isinstance(columns, list) and "message_id" in columns:
        for row in value.get("rows", []):
            cells = row if isinstance(row, dict) else dict(zip(columns, row, strict=True))
            number, collection = cells.get("message_id"), cells.get("chat")
            origin = cells.get("chat_id", chat_id)
            if not isinstance(collection, str) and isinstance(origin, int) and origin < 0:
                collection = f"telegram:{origin}"
            if isinstance(collection, str) and type(number) is int:
                sources.add(message_source_id(collection, number))
    return sources | set().union(
        *(
            _result_sources(key, chat_id) | _result_sources(item, chat_id)
            for key, item in value.items()
            if key not in {"text", "content", "query", "snippet", "sql"}
        )
    )


class ReplyTrace(BaseMiddleware):
    @override
    def __init__(self, event: BaseEvent, context: Context) -> None:
        super().__init__(event, context)
        self._sources: set[str] = set()
        self._model_calls = 0
        self._input_chars: int | None = None
        self._prompt_tokens: float | None = None

    def _result_fields(self, result: ToolResultType) -> dict[str, object]:
        if not isinstance(result, (ToolResultEvent, ToolErrorEvent)):
            return {}
        texts = [
            part.content for part in result.result.parts if isinstance(part, TextInput)
        ]
        sources: set[str] = set()
        for text in texts:
            payload = text
            if payload.startswith('<external_input source="'):
                payload = payload.partition("\n")[2].rsplit("\n</external_input>", 1)[0]
            with suppress(ValueError, TypeError, RecursionError):
                chat = _request(__ctx__=self.context).get("chat_id")
                sources.update(
                    _result_sources(
                        json.loads(payload), chat if isinstance(chat, int) else None
                    )
                )
        fields: dict[str, object] = {
            "result_chars": sum(len(text) for text in texts),
            "source_count": len(sources),
            "new_source_count": len(sources - self._sources),
        }
        self._sources.update(sources)
        return fields

    @override
    async def on_llm_call(
        self, call_next: LLMCall, events: Sequence[BaseEvent], context: Context
    ) -> ModelResponse:
        fields = _request(__ctx__=context)
        parts = [
            part
            for event in events
            if isinstance(event, (ModelRequest, ToolResultEvent))
            for part in (
                event.parts if isinstance(event, ModelRequest) else event.result.parts
            )
        ]
        fields.update(
            history_events=len(events),
            input_chars=sum(
                len(part.content) for part in parts if isinstance(part, TextInput)
            ),
            image_count=sum(
                isinstance(part, BinaryInput) and part.kind == BinaryType.IMAGE
                for part in parts
            ),
        )
        self._model_calls += 1
        fields["model_call_index"] = self._model_calls
        current_chars = sum(
            len(part.content) for part in parts if isinstance(part, TextInput)
        )
        fields["input_chars_delta"] = (
            current_chars - self._input_chars if self._input_chars is not None else None
        )
        self._input_chars = current_chars
        started = perf_counter()
        logger.info("AI model call started", extra=fields)
        try:
            response = await call_next(events, context)
        except asyncio.CancelledError:
            logger.info(
                "AI model call completed",
                extra=_outcome_fields(started, fields, "cancelled"),
            )
            raise
        except Exception as error:
            logger.exception(
                "AI model call completed",
                extra={
                    **_outcome_fields(started, fields, "error"),
                    **exception_fields(error),
                },
            )
            raise
        cached = response.usage.cache_read_input_tokens
        prompt = response.usage.prompt_tokens
        prompt_delta = (
            prompt - self._prompt_tokens
            if prompt is not None and self._prompt_tokens is not None
            else None
        )
        self._prompt_tokens = prompt
        logger.info(
            "AI model call completed",
            extra={
                **_outcome_fields(started, fields, "ok"),
                **_output_fields(response),
                "model": response.model,
                "prompt_tokens": response.usage.prompt_tokens,
                "prompt_tokens_delta": prompt_delta,
                "completion_tokens": response.usage.completion_tokens,
                "thinking_tokens": response.usage.thinking_tokens,
                "cached_input_tokens": cached,
                "cache_hit": cached > 0 if cached is not None else None,
                "cached_input_percent": round(100 * cached / prompt, 2)
                if cached is not None and prompt is not None and prompt > 0
                else None,
            },
        )
        return response

    @override
    async def on_tool_execution(
        self, call_next: ToolExecution, event: ToolCallEvent, context: Context
    ) -> ToolResultType:
        fields = {
            **_request(__ctx__=context),
            "tool_name": event.name,
            "tool_call_id": event.id,
            **_argument_fields(event),
        }
        started = perf_counter()
        logger.info("AI tool call started", extra=fields)
        try:
            result = await call_next(event, context)
        except asyncio.CancelledError:
            logger.info(
                "AI tool call completed",
                extra=_outcome_fields(started, fields, "cancelled"),
            )
            raise
        except (MemoryInputError, ValidationError) as error:
            logger.warning(
                "AI tool call completed",
                extra={
                    **_outcome_fields(started, fields, "error"),
                    **exception_fields(error),
                },
            )
            raise
        except Exception as error:
            logger.exception(
                "AI tool call completed",
                extra={
                    **_outcome_fields(started, fields, "error"),
                    **exception_fields(error),
                },
            )
            raise
        fields.update(self._result_fields(result))
        if isinstance(result, ToolErrorEvent):
            extra = {
                **_outcome_fields(started, fields, "error"),
                **exception_fields(result.error),
            }
            if isinstance(result.error, (MemoryInputError, ValidationError)):
                logger.warning("AI tool call completed", extra=extra)
            else:
                logger.error("AI tool call completed", extra=extra)
            if event.name in {
                "search_memory",
                "chat_history",
                "read_sources",
                "retry_image",
            }:
                return _memory_failure(event, result.error)
        else:
            logger.info(
                "AI tool call completed", extra=_outcome_fields(started, fields, "ok")
            )
        return result
