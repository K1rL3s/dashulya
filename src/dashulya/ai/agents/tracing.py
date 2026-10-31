"""Request-correlated model and tool timings without conversation payloads."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from time import perf_counter
from typing import Annotated, override

from ag2 import Context, Inject
from ag2.events import (
    BaseEvent,
    BinaryInput,
    BinaryType,
    ModelRequest,
    ModelResponse,
    TextInput,
    ToolCallEvent,
    ToolErrorEvent,
    ToolResult,
)
from ag2.middleware.base import BaseMiddleware, LLMCall, ToolExecution, ToolResultType
from fast_depends import inject
from pydantic import ValidationError

from dashulya.ai.tools.web import WebInputError
from dashulya.domain.memory import MemoryAccess, MemoryInputError
from dashulya.observability import exception_fields

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
) -> dict[str, object]:
    if isinstance(access, MemoryAccess):
        return {"chat_id": access.chat_id, "message_id": access.before_id}
    return {}


# AG2 calls middleware hooks directly, outside its annotation resolver.
_request: Callable[..., dict[str, object]] = inject(_request_fields, cast=False)


def _outcome_fields(
    started: float, fields: dict[str, object], outcome: str
) -> dict[str, object]:
    return {
        **fields,
        "duration_ms": round((perf_counter() - started) * 1000),
        "outcome": outcome,
    }


class ReplyTrace(BaseMiddleware):
    @override
    async def on_llm_call(
        self, call_next: LLMCall, events: Sequence[BaseEvent], context: Context
    ) -> ModelResponse:
        fields = _request(__ctx__=context)
        parts = [
            part
            for event in events
            if isinstance(event, ModelRequest)
            for part in event.parts
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
        logger.info(
            "AI model call completed",
            extra={
                **_outcome_fields(started, fields, "ok"),
                "model": response.model,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "thinking_tokens": response.usage.thinking_tokens,
                "cached_input_tokens": response.usage.cache_read_input_tokens,
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
        except (MemoryInputError, ValidationError, WebInputError) as error:
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
        if isinstance(result, ToolErrorEvent):
            extra = {
                **_outcome_fields(started, fields, "error"),
                **exception_fields(result.error),
            }
            if isinstance(
                result.error, (MemoryInputError, ValidationError, WebInputError)
            ):
                logger.warning("AI tool call completed", extra=extra)
            else:
                logger.error("AI tool call completed", extra=extra)
            if event.name in {"search_memory", "chat_history"}:
                return _memory_failure(event, result.error)
        else:
            logger.info(
                "AI tool call completed", extra=_outcome_fields(started, fields, "ok")
            )
        return result
