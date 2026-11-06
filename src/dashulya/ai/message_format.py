"""One compact message format for immediate context and retrieved evidence."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from ag2.events import ModelMessage, ModelRequest, ModelResponse, TextInput

from dashulya.domain.memory import (
    MemoryMessage,
    OriginalMessage,
    TruncatedThread,
    message_source_id,
    message_url_template,
)

if TYPE_CHECKING:
    from datetime import date

type Message = MemoryMessage | OriginalMessage
type InputSource = Literal["external_user", "bot", "owner", "web", "memory"]


def external_input(text: str, source: InputSource) -> str:
    """Label application-owned provenance without changing the source text."""
    return f'<external_input source="{source}">\n{text}\n</external_input>'


def input_source(
    user_id: int | None,
    *,
    owner_id: int | None = None,
    is_bot: bool = False,
) -> InputSource:
    """Only numeric sender identity and verified Telegram flags establish a label."""
    if is_bot:
        return "bot"
    if user_id is not None and user_id == owner_id:
        return "owner"
    return "external_user"


def format_local_time(local: datetime, *, include_date: bool = False) -> str:
    """Format a converted send time, disambiguating repeated local hours."""
    text = local.strftime("%d %B %Y (%A) %H:%M:%S" if include_date else "%H:%M:%S")
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        offset = local.strftime("%z")
        text += f" (UTC{offset[:3]}:{offset[3:]})"
    return text


def _sender(message: Message) -> int | str:
    # An absent numeric ID cannot establish that two messages share a sender.
    return (
        message.author_id
        if message.author_id is not None
        else f"unknown_{message.message_id}"
    )


def _reference(message: Message) -> str:
    if isinstance(message, OriginalMessage):
        return message.source_id
    return message_source_id(f"telegram:{message.chat_id}", message.message_id)


def _message_metadata(
    message: Message,
    bot_id: int | None,
    cut: TruncatedThread | None,
    author_users: Mapping[int, int],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "message_id": message.message_id,
        "user_id": _sender(message),
        "user": message.author,
    }
    if message.reply_to_id is not None:
        metadata["reply_to"] = message.reply_to_id
    if message.username:
        metadata["username"] = message.username.lstrip("@")
    if message.is_bot is True:
        metadata["bot"] = True
    author_user = (
        author_users.get(message.author_id) if message.author_id is not None else None
    )
    if author_user is not None and author_user != message.author_id:
        metadata["author_user"] = author_user
    if message.media_type:
        metadata["attachment"] = message.media_type
    # Tool text has no native assistant-role event to carry authorship.
    if (
        bot_id is not None
        and isinstance(message, OriginalMessage)
        and message.author_id == bot_id
    ):
        metadata["role"] = "assistant"
    if cut is not None:
        metadata["truncated_thread"] = {
            "root_id": cut.root_id,
            "remaining_hops": cut.remaining_hops,
            "boundary_source_id": _reference(message),
            **({"source_id": cut.source_id} if cut.source_id is not None else {}),
        }
    return metadata


def _format_messages(
    messages: Sequence[Message],
    *,
    timezone: ZoneInfo,
    bot_id: int | None,
    owner_id: int | None = None,
    interlocutor_id: int | None = None,
    author_users: Mapping[int, int] | None = None,
    truncated_threads: Sequence[TruncatedThread] = (),
    label: str = "Saved group context",
) -> list[dict[str, str]]:
    """Stable chat metadata and self-contained message records with native roles."""
    if not messages:
        return []
    latest = messages[-1]
    chat = json.dumps(
        {
            "id": latest.chat_id,
            "title": latest.chat_title,
            "username": latest.chat_username,
            "message_url_template": message_url_template(
                latest.chat_id, latest.chat_username
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = [
        {
            "role": "user",
            "text": f"{label}:\nchat: {chat}",
        }
    ]
    cuts = {thread.boundary_id: thread for thread in truncated_threads}
    previous_day: date | None = None
    for index, message in enumerate(messages):
        if message.sent_at is not None and message.sent_at.utcoffset() is None:
            error_message = "Message send time must include a timezone."
            raise ValueError(error_message)
        local = message.sent_at.astimezone(timezone) if message.sent_at else None
        lines = []
        if local is None:
            if index == 0 or previous_day is not None:
                lines.append("Unknown send date")
        elif local.date() != previous_day:
            lines.append(f"{local:%d %B %Y (%A)}")
        if index == len(messages) - 1 and type(interlocutor_id) is int:
            lines.append(f"Interlocutor user id: {interlocutor_id}")
        metadata = _message_metadata(
            message, bot_id, cuts.get(message.message_id), author_users or {}
        )
        lines.append(
            f"{format_local_time(local) if local else 'Unknown time'} "
            + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        )
        assistant = bot_id is not None and message.author_id == bot_id
        text = (
            message.text
            if assistant
            else external_input(
                message.text,
                input_source(
                    message.author_id,
                    owner_id=owner_id,
                    is_bot=message.is_bot is True
                    or (bot_id is not None and message.author_id == bot_id),
                ),
            )
        )
        result.append({
            "role": "assistant" if assistant else "user",
            "text": "\n".join(lines) + "\n" + text,
        })
        previous_day = local.date() if local else None
    return result


def _original(source: dict[str, Any]) -> OriginalMessage:
    return OriginalMessage(
        collection=source.get("chat", "unknown"),
        chat_id=source.get("chat_id"),
        message_id=source["message_id"],
        text=source["text"],
        sent_at=datetime.fromisoformat(source["sent_at"])
        if source.get("sent_at")
        else None,
        author_id=source.get("author_id", source.get("user_id")),
        author=source["author"],
        chat_title=source.get("chat_title", ""),
        chat_username=source.get("chat_username"),
        topic_id=source.get("topic_id"),
        reply_to_id=source.get("reply_to_message_id", source.get("reply_to_id")),
        edited_at=source.get("edited_at"),
        media_type=source.get("media_type"),
        url=source.get("url"),
        provenance=source.get("provenance", "unknown"),
        username=source.get("username"),
        is_bot=source.get("is_bot"),
    )


def _source_order(source: dict[str, Any]) -> tuple[datetime, int]:
    instant = (
        datetime.fromisoformat(source["sent_at"])
        if source.get("sent_at")
        else datetime.min.replace(tzinfo=UTC)
    )
    if instant.utcoffset() is None:
        error_message = "Message send time must include a timezone."
        raise ValueError(error_message)
    return instant, source["message_id"]


class MessageFormatter:
    def __init__(
        self,
        timezone: str = "Europe/Moscow",
        bot_id: int | None = None,
        *,
        author_users: Mapping[int, int] | None = None,
        owner_id: int | None = None,
    ) -> None:
        self.timezone = ZoneInfo(timezone)
        self.bot_id = bot_id
        self.owner_id = owner_id
        self._author_users = dict(author_users or {})

    def context(
        self,
        messages: Sequence[MemoryMessage],
        *,
        interlocutor_id: int | None = None,
        truncated_threads: Sequence[TruncatedThread] = (),
    ) -> list[dict[str, str]]:
        return _format_messages(
            messages,
            timezone=self.timezone,
            bot_id=self.bot_id,
            owner_id=self.owner_id,
            interlocutor_id=interlocutor_id,
            author_users=self._author_users,
            truncated_threads=truncated_threads,
        )

    def events(
        self,
        messages: Sequence[MemoryMessage],
        *,
        interlocutor_id: int | None = None,
        truncated_threads: Sequence[TruncatedThread] = (),
    ) -> list[ModelRequest | ModelResponse]:
        """Render selected messages as AG2 events, including the current request.

        Chat metadata is a separate user event; bot messages retain assistant roles.
        This performs no history reads and does not create or modify a stream.
        """
        return [
            ModelResponse(ModelMessage(item["text"]))
            if item["role"] == "assistant"
            else ModelRequest([TextInput(item["text"])])
            for item in self.context(
                messages,
                interlocutor_id=interlocutor_id,
                truncated_threads=truncated_threads,
            )
        ]

    def result(
        self, result: dict[str, Any], *, interlocutor_id: int | None = None
    ) -> dict[str, Any]:
        """Keep result semantics, replacing repeated originals with source references."""
        sources: dict[str, dict[str, Any]] = {}

        def collect(value: Any) -> Any:
            if isinstance(value, list):
                return [collect(item) for item in value]
            if not isinstance(value, dict):
                return value
            if {"message_id", "text", "author", "sent_at"} <= value.keys():
                reference = value.get("source_id", f"message:{value['message_id']}")
                saved = sources.get(reference)
                # Overlapping hits/context can contain a longer preview of this original.
                if saved is None or len(value["text"]) > len(saved["text"]):
                    sources[reference] = value
                return reference
            return {
                key: collect(item)
                for key, item in value.items()
                # For a message hit, memory is a duplicate rendering of its sources.
                if not (key == "memory" and value.get("kind") == "message")
            }

        output: dict[str, Any] = collect(result)
        if not sources:
            return output
        cuts = output.pop("truncated_threads", {})
        chats: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for reference, source in sources.items():
            chats.setdefault(source.get("chat", "unknown"), []).append((
                reference,
                source,
            ))
        blocks = []
        for chat, entries in chats.items():
            entries.sort(key=lambda item: _source_order(item[1]))
            messages = [_original(source) for _, source in entries]
            blocks.append({
                "chat": chat,
                "chat_id": messages[0].chat_id,
                "titles": list(
                    dict.fromkeys(
                        message.chat_title for message in messages if message.chat_title
                    )
                ),
                "usernames": list(
                    dict.fromkeys(
                        message.chat_username
                        for message in messages
                        if message.chat_username
                    )
                ),
                "text": "\n===\n".join(
                    item["text"]
                    for item in _format_messages(
                        messages,
                        timezone=self.timezone,
                        bot_id=self.bot_id,
                        owner_id=self.owner_id,
                        interlocutor_id=interlocutor_id,
                        author_users=self._author_users,
                        truncated_threads=[
                            TruncatedThread(**cuts[reference])
                            for reference, _ in entries
                            if reference in cuts
                        ],
                        label="Saved messages",
                    )
                ),
                "sources": {
                    reference: self._metadata(source) for reference, source in entries
                },
            })
        output["message_blocks"] = blocks
        return output

    @staticmethod
    def _metadata(source: dict[str, Any]) -> dict[str, Any]:
        metadata = {
            key: source[key]
            for key in [
                "message_id",
                "url",
                "provenance",
                "topic_id",
                "edited_at",
                "archive_key",
            ]
            if source.get(key) is not None
        }
        if source.get("truncated"):
            metadata.update({
                key: source[key]
                for key in ["offset", "text_length", "truncated", "next_offset"]
                if key in source
            })
        return metadata
