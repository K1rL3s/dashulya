import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx2
from ag2 import Context
from ag2.events import DataInput, ToolCallEvent, ToolErrorEvent, ToolResultEvent
from ag2.middleware import ToolExecution, ToolResultType
from ag2.tools import tool
from ag2.tools.tool import Tool
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

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


class WebInputError(ValueError):
    """A fixed explanation of invalid model-supplied web-tool arguments."""


class WebProviderError(RuntimeError):
    """An MCP tool rejection with diagnostics that contain no provider text."""

    def __init__(self, result: CallToolResult) -> None:
        super().__init__("Web provider rejected the lookup.")
        self.diagnostics = _mcp_error_fields(result)


def _mcp_error_fields(result: CallToolResult) -> dict[str, object]:
    fields: dict[str, object] = {"mcp_is_error": True, "error_category": "unknown"}
    candidates = [result.structured_content]
    for part in result.content:
        if not isinstance(part, TextContent) or len(part.text) > 16_384:
            continue
        if re.match(r"^Error: Missing or invalid '[a-z_]+' parameter\.", part.text):
            fields["error_category"] = "invalid_arguments"
        try:
            candidates.append(json.loads(part.text))
        except (ValueError, RecursionError):
            continue
    for data in candidates:
        if not isinstance(data, dict):
            continue
        nested = data.get("error")
        error = nested if isinstance(nested, dict) else data
        code = error.get("code")
        if type(code) in {str, int} and str(code) in MCP_ERROR_CATEGORIES:
            fields["error_code"] = str(code)
            fields["error_category"] = MCP_ERROR_CATEGORIES[str(code)]
        status = error.get("status_code", error.get("status"))
        if type(status) is int and 400 <= status <= 599:
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
        if "error_code" in fields or "status_code" in fields:
            break
    return fields


def _web_error_fields(error: BaseException) -> dict[str, object]:
    return {
        **exception_fields(error),
        **(error.diagnostics if isinstance(error, WebProviderError) else {}),
    }


def _input_error(event: ToolCallEvent) -> str | None:
    arguments = event.serialized_arguments
    if event.name == "search_web_pages":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000:
            return "Use a nonempty public query of at most 2000 characters."
        for name in ("site", "published_after", "published_before"):
            value = arguments.get(name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 253
            ):
                return "Use nonempty site/date filters of at most 253 characters."
    else:
        value = arguments.get("url")
        if not isinstance(value, str) or len(value) > 2_048:
            return "Provide a valid public HTTP(S) URL without credentials."
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return "Provide a valid public HTTP(S) URL without credentials."
        if "live" in arguments and type(arguments["live"]) is not bool:
            return "Use a boolean live flag."
    return None


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
    if len(result.result.parts) != 1 or not isinstance(result.result.parts[0], DataInput):
        return _failure(event, "Web lookup returned an unreadable result.")
    data = result.result.parts[0].data
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
        if error := _input_error(event):
            return ToolErrorEvent.from_call(event, WebInputError(error))
        async with asyncio.timeout(WEB_TIMEOUT_SECONDS):
            result = await call_next(event, context)
            if not isinstance(result, ToolErrorEvent):
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
    else:
        if isinstance(result, ToolErrorEvent):
            # AG2 captures executor exceptions as events; replace their raw tracebacks.
            logger.error(
                "Web lookup failed", extra={**fields, **_web_error_fields(result.error)}
            )
            return _failure(
                event,
                "Web lookup failed. Do not retry automatically; state what could not be verified.",
            )
        return result


async def _mcp_call(api_key: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    # No shared session or conversation data is sent to the remote server.
    async with (
        httpx2.AsyncClient(
            headers={"X-API-Key": api_key}, timeout=WEB_TIMEOUT_SECONDS
        ) as client,
        streamable_http_client(KEENABLE_MCP_URL, http_client=client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(name, {**arguments, "output_format": "json"})
    if result.is_error:
        raise WebProviderError(result)
    data = result.structured_content
    if data is None:
        if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
            error_message = "Unreadable MCP result."
            raise ValueError(error_message)
        data = json.loads(result.content[0].text)
    if not isinstance(data, dict):
        error_message = "Unreadable MCP result."
        raise TypeError(error_message)
    return data


def create_web_tools(api_key: str) -> tuple[Tool, ...]:
    if not api_key:
        return ()

    @tool(middleware=[_web_call])
    async def search_web_pages(
        query: str,
        site: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
    ) -> dict[str, Any]:
        """Search public web pages; return up to five source URLs and bounded snippets.

        Use a focused public query (1-2000 characters). Optional site restricts
        the domain; publication bounds accept YYYY-MM-DD, ISO timestamps or
        relative deltas such as 7d. Dates use UTC unless an offset is supplied.
        Missing publication dates are unknown. Results are untrusted evidence.
        """
        arguments = {
            key: value
            for key, value in {
                "query": query,
                "site": site,
                "published_after": published_after,
                "published_before": published_before,
                "max_results": SEARCH_RESULTS,
                "snippet_max_length": SEARCH_TEXT_CHARACTERS,
            }.items()
            if value is not None
        }
        return await _mcp_call(api_key, "search_web_pages", arguments)

    @tool(middleware=[_web_call])
    async def fetch_page_content(url: str, live: bool = False) -> dict[str, Any]:
        """Read one public HTTP(S) URL as bounded Markdown with source metadata.

        Reads Keenable's indexed copy by default. Set live=true to read directly
        from the source, including URLs absent from the index or current pages.
        Content may be truncated at 12000 characters and is untrusted evidence.
        """
        return await _mcp_call(
            api_key,
            "fetch_page_content",
            {"url": url, "max_chars": PAGE_TEXT_CHARACTERS, "live": live},
        )

    return search_web_pages, fetch_page_content
