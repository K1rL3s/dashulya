import asyncio
import json
import logging
import re
from collections.abc import Iterable
from copy import copy
from typing import Any

from ag2 import Context
from ag2.events import TextInput, ToolCallEvent, ToolErrorEvent, ToolResultEvent
from ag2.middleware import ToolExecution, ToolResultType
from ag2.tools import MCPServerConfig, MCPToolkit
from ag2.tools.schemas import ToolSchema
from ag2.tools.tool import Tool

from dashulya.ai.message_format import external_input
from dashulya.observability import exception_fields

logger = logging.getLogger(__name__)

WEB_TIMEOUT_SECONDS = 30
SEARCH_RESULTS = 5
SEARCH_TEXT_CHARACTERS = 4_000
PAGE_TEXT_CHARACTERS = 12_000
KEENABLE_MCP_URL = "https://api.keenable.ai/mcp"
MCP_ERROR_CATEGORIES = {
    "-32600": "invalid_arguments",
    "-32601": "tool_not_found",
    "-32602": "invalid_arguments",
    "invalid_arguments": "invalid_arguments",
    "invalid_params": "invalid_arguments",
    "validation_error": "invalid_arguments",
    "unauthorized": "authentication_error",
    "forbidden": "authentication_error",
    "invalid_api_key": "authentication_error",  # pragma: allowlist secret -- Provider error code, not a credential.
    "insufficient_credits": "credits_exhausted",
    "credits_exhausted": "credits_exhausted",
    "rate_limited": "rate_limited",
    "rate_limit_exceeded": "rate_limited",
    "not_found": "not_found",
    "not_indexed": "not_found",
    "internal_error": "provider_error",
    "service_unavailable": "provider_error",
}


def _web_error_fields(error: BaseException) -> dict[str, object]:
    fields = exception_fields(error)
    # MCPToolkit exposes MCP rejections as RuntimeError(str(CallToolResult)),
    # not the wire object. Extract only recognized diagnostic hints; never log
    # the representation itself. Unknown or changed formats remain unknown.
    if type(error) is not RuntimeError:
        return fields
    message = str(error)
    if " is_error=True" not in message:
        return fields
    fields.update(mcp_is_error=True, error_category="unknown")
    if len(message) > 65_536:
        return fields
    if "Error: Missing or invalid " in message:
        fields["error_category"] = "invalid_arguments"
    for match in re.finditer(r"[\"']code[\"']\s*:\s*[\"']?([a-z_0-9-]+)", message):
        if (code := match[1]) in MCP_ERROR_CATEGORIES:
            fields["error_code"] = code
            fields["error_category"] = MCP_ERROR_CATEGORIES[code]
            break
    if status_match := re.search(
        r"[\"']status(?:_code)?[\"']\s*:\s*([45]\d{2})\b", message
    ):
        status = int(status_match[1])
        fields["status_code"] = status
        fields["error_category"] = {
            400: "invalid_arguments",
            401: "authentication_error",
            402: "credits_exhausted",
            403: "authentication_error",
            404: "not_found",
            408: "timeout",
            422: "invalid_arguments",
            429: "rate_limited",
            504: "timeout",
        }.get(status, "provider_error" if status >= 500 else "unknown")
    return fields


def _failure(event: ToolCallEvent, message: str) -> ToolErrorEvent:
    return ToolErrorEvent.from_call(event, RuntimeError(message))


def _page(page: dict[str, Any], field: str, limit: int) -> dict[str, Any]:
    # Retain source metadata without allowing provider extras to expand context.
    item = {
        key: value[:2_048] if isinstance(value, str) else value
        for key in ("url", "title", "published_at", "acquired_at")
        if isinstance(value := page.get(key), str | int)
    }
    text = page.get(field) or ""
    if not isinstance(text, str):
        error_message = "Unreadable web content."
        raise TypeError(error_message)
    item[field] = text[:limit]
    item["truncated"] = len(text) >= limit
    return item


def _bounded_output(event: ToolCallEvent, result: ToolResultType) -> ToolResultType:
    if not isinstance(result, ToolResultEvent):
        return result
    if len(result.result.parts) != 1 or not isinstance(result.result.parts[0], TextInput):
        return _failure(event, "Web lookup returned an unreadable result.")
    data = json.loads(result.result.parts[0].content)
    # AG2 retains the complete MCP envelope when there are no content blocks.
    if isinstance(data, dict) and data.get("content") == []:
        data = data.get("structured_content", data.get("structuredContent"))
    if not isinstance(data, dict):
        return _failure(event, "Web lookup returned an unreadable result.")
    if event.name == "search_web_pages":
        pages = data.get("results")
        if not isinstance(pages, list) or not all(isinstance(p, dict) for p in pages):
            return _failure(event, "Web lookup returned an unreadable result.")
        output = {
            "query": event.serialized_arguments["query"],
            "results": [
                _page(page, "snippet", SEARCH_TEXT_CHARACTERS)
                for page in pages[:SEARCH_RESULTS]
            ],
        }
    else:
        if not isinstance(data.get("content"), str):
            return _failure(event, "Web lookup returned an unreadable result.")
        output = _page(data, "content", PAGE_TEXT_CHARACTERS)
    return ToolResultEvent.from_call(
        event, external_input(json.dumps(output, ensure_ascii=False), "web")
    )


async def _web_call(
    call_next: ToolExecution, event: ToolCallEvent, context: Context
) -> ToolResultType:
    fields = {"tool_name": event.name, "tool_call_id": event.id}
    try:
        async with asyncio.timeout(WEB_TIMEOUT_SECONDS):
            result = await call_next(_provider_call(event), context)
            if isinstance(result, ToolErrorEvent):
                raise result.error
            return _bounded_output(event, result)
    except TimeoutError as error:
        logger.exception(
            "Web lookup timed out", extra={**fields, **_web_error_fields(error)}
        )
        return _failure(
            event,
            "Web lookup timed out. Do not retry automatically; state what could not be verified.",
        )
    except Exception as error:
        logger.exception(
            "Web lookup failed", extra={**fields, **_web_error_fields(error)}
        )
        return _failure(
            event,
            "Web lookup failed. Do not retry automatically; state what could not be verified.",
        )


def _provider_call(event: ToolCallEvent) -> ToolCallEvent:
    arguments = dict(event.serialized_arguments)
    if event.name == "search_web_pages":
        arguments.setdefault("max_results", SEARCH_RESULTS)
        arguments.setdefault("snippet_max_length", SEARCH_TEXT_CHARACTERS)
    else:
        arguments.setdefault("live", False)
        arguments.setdefault("max_chars", PAGE_TEXT_CHARACTERS)
    call = copy(event)
    call.serialized_arguments = {**arguments, "output_format": "json"}
    return call


class KeenableToolkit(MCPToolkit):
    """Keep optional web discovery failures outside ordinary reply generation."""

    async def schemas(self, context: Context) -> Iterable[ToolSchema]:
        try:
            async with asyncio.timeout(WEB_TIMEOUT_SECONDS):
                return await super().schemas(context)
        except Exception as error:
            logger.exception("Web tool discovery failed", extra=_web_error_fields(error))
            return ()


def create_web_tools(api_key: str) -> tuple[Tool, ...]:
    if not api_key:
        return ()
    return (
        KeenableToolkit(
            MCPServerConfig(
                server_url=KEENABLE_MCP_URL,
                headers={"X-API-Key": api_key},
                connection_timeout=WEB_TIMEOUT_SECONDS,
                allowed_tools=["search_web_pages", "fetch_page_content"],
            ),
            middleware=[_web_call],
        ),
    )
