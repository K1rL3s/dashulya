import asyncio
import json
from dataclasses import dataclass, field
from typing import Annotated, Any

from ag2 import Inject
from ag2.events import BinaryInput, ImageInput, ToolResult
from ag2.tools import tool
from dishka_ag2 import FromDishka, inject
from pydantic import Field

from dashulya.ai.message_format import external_input
from dashulya.domain.memory import (
    ContextImages,
    HistoryReader,
    MemoryMessage,
    OriginalMessage,
    message_source_id,
)
from dashulya.domain.messages import ImageAttachment, ImageOmission


@dataclass
class HistoryImageTurn:
    remaining_bytes: int
    request_message_id: int | None
    images: ContextImages
    chat_id: int
    sources: dict[int, MemoryMessage | OriginalMessage] = field(default_factory=dict)
    attempts: dict[int, int] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def retry(
        self, message_id: int, timeout_seconds: int, history: HistoryReader
    ) -> ToolResult:
        async with self.lock:
            if self.attempts.get(message_id, 0) >= 3:
                return ToolResult(
                    external_input(
                        json.dumps({
                            "message_id": message_id,
                            "attempts": 3,
                            "remaining_attempts": 0,
                            "status": "retry_limit_reached",
                        }),
                        "memory",
                    )
                )
            attempt = self.attempts.get(message_id, 0) + 1
            self.attempts[message_id] = attempt
            data: dict[str, Any] = {
                "message_id": message_id,
                "attempts": attempt,
                "remaining_attempts": 3 - attempt,
            }
            source = self.sources.get(message_id)
            if source is None:
                result = await history.query(
                    chat=f"telegram:{self.chat_id}",
                    min_message_id=message_id,
                    max_message_id=message_id,
                    limit=1,
                    parent_hops=0,
                )
                if result["messages"]:
                    source = await history.original(result["messages"][0]["source_id"])
                    if source is not None:
                        self.sources[message_id] = source
            loaded: tuple[ImageAttachment | ImageOmission, ...] = ()
            if source is None:
                data["status"] = "not_found"
            elif source.image_file is None:
                data["status"] = "no_saved_reference"
            elif self.remaining_bytes <= 0:
                data["status"] = "too_large"
            else:
                reference = (
                    source.source_id
                    if isinstance(source, OriginalMessage)
                    else message_source_id(
                        f"telegram:{source.chat_id}", source.message_id
                    )
                )
                data["source_id"] = reference
                if reference in self.seen:
                    data["status"] = "already_supplied"
                else:
                    loaded = await self.images.retry_image(
                        source,
                        timeout_seconds=timeout_seconds,
                        max_bytes=self.remaining_bytes,
                        request_message_id=self.request_message_id,
                    )
                    self.remaining_bytes -= sum(
                        len(image.data)
                        for image in loaded
                        if isinstance(image, ImageAttachment)
                    )
                    for image in loaded:
                        if isinstance(image, ImageOmission):
                            data["status"] = image.reason
                        else:
                            data.update(
                                status="loaded",
                                untrusted=True,
                                preview_kind=image.preview_kind,
                                first_frame=image.first_frame,
                            )
                            self.seen.add(reference)
            return ToolResult(
                external_input(json.dumps(data, ensure_ascii=False), "memory"),
                *(
                    ImageInput(data=image.data, media_type=image.media_type)
                    for image in loaded
                    if isinstance(image, ImageAttachment)
                ),
            )

    async def load(
        self, source: OriginalMessage
    ) -> tuple[ImageAttachment | ImageOmission, ...]:
        async with self.lock:
            if source.source_id in self.seen:
                return ()
            if self.remaining_bytes <= 0:
                return (ImageOmission(source.message_id, "too_large"),)
            loaded = await self.images.load_context(
                (source,),
                request_message_id=self.request_message_id,
                max_bytes=self.remaining_bytes,
            )
            self.remaining_bytes -= sum(
                len(image.data) for image in loaded if isinstance(image, ImageAttachment)
            )
            if any(isinstance(image, ImageAttachment) for image in loaded):
                self.seen.add(source.source_id)
            return loaded


@tool
@inject
async def retry_image(
    message_id: Annotated[int, Field(gt=0, strict=True)],
    timeout_seconds: Annotated[int, Field(ge=1, le=120, strict=True)] = 10,
    *,
    history: FromDishka[HistoryReader],
    image_turn: Annotated[
        HistoryImageTurn | None, Inject("history_image_turn", default=None)
    ],
) -> ToolResult:
    """Retry an image or media preview by its Telegram message ID in this chat.

    Choose timeout_seconds from 1 to 120 (default 10) for the whole attempt.
    Failed-download cache entries are bypassed. At most three calls per message
    are allowed within this agent session, including failed calls. "already_supplied" means the image is
    already available from a tool result. Never guess contents when unavailable.
    """
    if image_turn is None:
        return ToolResult(
            external_input(
                json.dumps({"message_id": message_id, "status": "loading_unavailable"}),
                "memory",
            )
        )
    return await image_turn.retry(message_id, timeout_seconds, history)


def _table_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep exact cell values while sending each column name only once."""
    return {
        **result,
        "results": [
            {
                **item,
                "rows": [
                    [row[column] for column in item["columns"]] for row in item["rows"]
                ],
            }
            if "columns" in item
            else item
            for item in result["results"]
        ],
    }


async def _load_image(
    item: dict[str, Any],
    history: HistoryReader,
    image_turn: HistoryImageTurn | None,
) -> list[BinaryInput]:
    attachments = []
    if len(item.get("rows", [])) == 1 and isinstance(
        item["rows"][0].get("source_id"), str
    ):
        source = await history.original(item["rows"][0]["source_id"])
        if source is not None and source.media_type is not None:
            if source.image_file is None:
                item["image"] = {
                    "source_id": source.source_id,
                    "status": "no_saved_reference",
                }
            elif image_turn is None:
                item["image"] = {
                    "source_id": source.source_id,
                    "status": "loading_unavailable",
                }
            else:
                loaded = await image_turn.load(source)
                item["image"] = {
                    "source_id": source.source_id,
                    "status": "already_supplied",
                }
                for image in loaded:
                    if isinstance(image, ImageOmission):
                        item["image"]["status"] = image.reason
                    else:
                        item["image"].update(
                            status="loaded",
                            untrusted=True,
                            preview_kind=image.preview_kind,
                            first_frame=image.first_frame,
                        )
                        attachments.append(
                            ImageInput(data=image.data, media_type=image.media_type)
                        )
    return attachments


@tool
@inject
async def chat_history(
    sql: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=5,
            description="Independent PostgreSQL SELECT or WITH ... SELECT queries over messages. Use a one-element list for one query.",
        ),
    ],
    *,
    history: FromDishka[HistoryReader],
    image_turn: Annotated[
        HistoryImageTurn | None, Inject("history_image_turn", default=None)
    ],
) -> ToolResult:
    """Read saved messages, reply threads, participants and exact counts.

    Schema (NULL means unknown):
    ```sql
    messages (
        source_id TEXT NOT NULL,            -- Canonical ID for exact reads and media.
        chat TEXT NOT NULL,                 -- Canonical key, e.g. 'telegram:-100123'.
        chat_id BIGINT NULL,                -- Telegram chat ID; NULL for unbound archives.
        message_id BIGINT NOT NULL,          -- Telegram message ID.
        text TEXT NOT NULL,                 -- Original text/caption; may be empty.
        sent_at TIMESTAMPTZ NULL,            -- Original send time.
        author_id BIGINT NULL,               -- Numeric sender ID.
        author TEXT NOT NULL,               -- Sender display name.
        username TEXT NULL,                 -- Sender handle.
        is_bot BOOLEAN NULL,
        chat_title TEXT NOT NULL,
        chat_username TEXT NULL,             -- Group handle without @, e.g. 'example_chat'.
        topic_id BIGINT NULL,                -- Forum topic ID.
        reply_to_message_id BIGINT NULL,     -- Parent message ID in the same chat/topic.
        edited_at TIMESTAMPTZ NULL,
        media_type TEXT NULL,               -- E.g. photo, sticker, animation.
        url TEXT NULL,                      -- Original message link.
        provenance TEXT NOT NULL,           -- E.g. 'live', 'imported'.
        archive_key TEXT NULL               -- Import reference.
    )
    ```
    In groups, messages already contains only the current group; omit chat-selection
    WHERE predicates. In owner-private conversations, narrow to the requested group
    with chat_id = -100123 or chat_username = 'example_chat'. Use chat only with an
    observed canonical key such as 'telegram:-100123' or 'archive:...'.

    Key: (chat, message_id). Join replies within that chat and use
    IS NOT DISTINCT FROM for nullable topic IDs. Timestamp literals need an offset.
    Group local days with (sent_at AT TIME ZONE 'Europe/Moscow')::date;
    substitute the requested timezone or the system timezone.
    CTEs, recursive CTEs and self-joins are supported.
    GROUP BY must include every selected nonaggregate column or expression;
    IDs do not imply other columns. For reply counts, aggregate replies in a CTE
    by chat, topic_id and reply_to_message_id, then join the originals.

    Batch independent reads in one call. Select only needed columns; use short
    text excerpts for discovery and full text for the chosen originals.
    Use ORDER BY and LIMIT (up to 50 rows, 32 uniquely named columns per query;
    48,000 result characters shared across the batch, 12,000 per cell).
    Results follow input order: query_index is 1-based; outcome=ok supplies
    columns and positional row arrays, row_count and truncated; outcome=error
    supplies error.
    Correct and retry failed queries individually. row_count counts returned rows;
    use COUNT(*) for totals. When truncated, page with ID bounds or OFFSET;
    continue long text with substring(text from 12001 for 12000).

    To view saved media, select one original's source_id with LIMIT 1;
    image.status reports whether an image or preview was supplied.

    Examples (replace sample IDs and handles with observed values):
    - Case 1: Read messages between two IDs, including both endpoints.
      {"sql": ["SELECT message_id,author,text,url FROM messages WHERE message_id BETWEEN 20000 AND 20100 ORDER BY message_id LIMIT 50"]}
    - Case 2: Read the thread containing a message, including saved sibling branches.
      {"sql": ["WITH RECURSIVE thread AS (SELECT * FROM messages WHERE message_id = 20998 UNION SELECT m.* FROM messages m JOIN thread t ON m.chat = t.chat AND m.topic_id IS NOT DISTINCT FROM t.topic_id AND ((m.message_id = t.reply_to_message_id AND m.message_id < t.message_id) OR (m.reply_to_message_id = t.message_id AND m.message_id > t.message_id))) SELECT message_id,reply_to_message_id,author,text,url FROM thread ORDER BY message_id LIMIT 50"]}
    - Case 3: Count messages containing a phrase in a fixed period.
      {"sql": ["SELECT count(*) AS matches FROM messages WHERE text ILIKE '%deployment%' AND sent_at >= TIMESTAMPTZ '2026-10-01 00:00:00+03' AND sent_at < TIMESTAMPTZ '2026-11-01 00:00:00+03'"]}
    - Case 4: Batch a participant's messages and an independent exact count.
      {"sql": ["SELECT message_id,sent_at,author,text,url FROM messages WHERE author_id = 42 AND text ILIKE '%deployment%' ORDER BY message_id DESC LIMIT 20", "SELECT count(*) AS matches FROM messages WHERE author_id = 42 AND text ILIKE '%deployment%'"]}
    - Case 5: Only in an owner-private conversation, read one group by its handle.
      {"sql": ["SELECT message_id,sent_at,author,text,url FROM messages WHERE chat_username = 'example_chat' ORDER BY sent_at DESC,message_id DESC LIMIT 20"]}
    """
    result = await history.sql(sql)
    attachments = []
    for item in result["results"]:
        attachments.extend(await _load_image(item, history, image_turn))
    text = external_input(
        json.dumps(_table_result(result), ensure_ascii=False, separators=(",", ":")),
        "memory",
    )
    return ToolResult(text, *attachments)
