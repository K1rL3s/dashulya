"""Read selected evidence without repeatedly sending the same original text."""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated, Any

from ag2 import Inject, Variable
from ag2.events import ToolResult
from ag2.tools import tool
from dishka_ag2 import FromDishka, inject
from pydantic import Field

from dashulya.ai.message_format import MessageFormatter, external_input
from dashulya.ai.tools.history import HistoryImageTurn, load_source_image
from dashulya.domain.memory import HistoryReader, OriginalMessage


@dataclass
class SourceReadTurn:
    supplied: dict[tuple[str, str], list[tuple[int, int]]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def remember(self, source: OriginalMessage, start: int, end: int) -> bool:
        key = (source.source_id, hashlib.sha256(source.text.encode()).hexdigest())
        ranges = self.supplied.setdefault(key, [])
        if any(a <= start and end <= b for a, b in ranges):
            return False
        ranges.append((start, end))
        return True


@tool
@inject
async def read_sources(
    source_ids: Annotated[list[str], Field(min_length=1, max_length=10)],
    include_replies: bool = False,
    include_images: bool = False,
    offset: Annotated[int, Field(ge=0, strict=True)] = 0,
    limit: Annotated[int, Field(ge=1, le=20, strict=True)] = 5,
    text_offset: Annotated[int, Field(ge=0, strict=True)] = 0,
    text_chars: Annotated[int, Field(ge=1, le=12000, strict=True)] = 3000,
    *,
    history: FromDishka[HistoryReader],
    formatter: FromDishka[MessageFormatter],
    turn: FromDishka[SourceReadTurn],
    user_id: Annotated[int | None, Variable(default=None)],
    image_turn: Annotated[
        HistoryImageTurn | None, Inject("history_image_turn", default=None)
    ],
) -> ToolResult:
    """Read 1-10 canonical source IDs from memory/search results in one batch.

    Each source has its own page of the original and, with include_replies, its
    descendants in the same chat/topic. limit is per source, offset skips that
    many rows per source; use each result's next_offset to continue selected
    branches. text_offset is a zero-based character offset; use next_offset in
    message metadata to continue long text. At most 48000 text characters are
    shared fairly across returned messages; text_chars is the per-message maximum.
    Already supplied text is referenced by ID, not sent again. A longer excerpt
    or a different character range remains readable. Prior tool results retain
    the text. include_images explicitly loads selected images/previews under the
    turn's shared byte budget. Missing/inaccessible sources report not_found.
    Reads cannot expand the session's trusted chat or time scope or reply targets.
    """
    async with turn.lock:
        pages = await history.read_sources(
            source_ids,
            include_replies=include_replies,
            offset=offset,
            limit=limit,
        )
        unique = {m.source_id: m for page in pages for m in page.messages}
        allowance = min(text_chars, 48000 // max(1, len(unique)))
        messages = []
        already = []
        for source in unique.values():
            item = source.render(offset=text_offset, max_chars=allowance)
            if turn.remember(source, text_offset, text_offset + len(item["text"])):
                messages.append(item)
            else:
                already.append(source.source_id)
        output: dict[str, Any] = {
            "results": [
                {
                    "source_id": page.source_id,
                    "status": "ok" if page.found else "not_found",
                    "messages": [m.source_id for m in page.messages],
                    "next_offset": page.next_offset,
                }
                for page in pages
            ],
            "messages": messages,
            "already_supplied": already,
        }
        attachments = []
        if include_images:
            for source in unique.values():
                media: dict[str, Any] = {}
                attachments.extend(await load_source_image(media, source, image_turn))
                if "image" in media:
                    output.setdefault("images", []).append(media["image"])
        return ToolResult(
            external_input(
                json.dumps(
                    formatter.result(output, interlocutor_id=user_id),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "memory",
            ),
            *attachments,
        )
