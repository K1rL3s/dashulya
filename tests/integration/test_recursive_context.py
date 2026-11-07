"""Production reply context rebuilt from durable delivery origins."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ag2 import Agent
from ag2.events import BinaryInput, ModelRequest

from dashulya.ai.dialogue import Dialogue
from dashulya.ai.message_format import MessageFormatter
from dashulya.database import Database
from dashulya.domain.memory import MemoryMessage
from dashulya.domain.messages import ImageAttachment, ReplyBatch
from dashulya.memory.store import MessageStore


def model_input(value: Any) -> Any:
    """Event creation timestamps are local bookkeeping, absent from model inputs."""
    if isinstance(value, dict):
        return {
            key: model_input(item) for key, item in value.items() if key != "created_at"
        }
    if isinstance(value, list):
        return [model_input(item) for item in value]
    return value


async def test_recursive_context_restores_previous_request_prefix_with_native_media(
    database: Database,
) -> None:
    store = MessageStore(database)
    for number in range(1, 63):
        bot = number in {20, 40, 60}
        await store.record(
            MemoryMessage(
                chat_id=-123,
                message_id=number,
                text=f"Message {number}",
                sent_at=datetime(2026, 11, 1, tzinfo=UTC) + timedelta(hours=number),
                author_id=123 if bot else 42,
                author="Dashulya" if bot else "Anna",
                chat_title="Group",
                reply_to_id=5 if bot else None,
                trigger_message_id={20: 19, 40: 38, 60: 58}.get(number),
                media_type="photo" if number in {10, 61} else None,
                quoted_text="Original fragment" if number == 58 else None,
            )
        )
    dialogue = Dialogue(
        Agent[ReplyBatch]("unused"),
        history=MessageStore(database),
        formatter=MessageFormatter(bot_id=123),
    )
    previous = await dialogue.select_context((-123, None), 58)
    current = await dialogue.select_context((-123, None), 62)
    assert [message.message_id for message in current.messages] == list(range(1, 63))
    images = (
        ImageAttachment(b"old", "image/png", 10),
        ImageAttachment(b"new", "image/png", 61),
    )
    prefix = [
        model_input(event.to_dict())
        for event in dialogue.context_events(previous, images=images)
    ]
    events = dialogue.context_events(current, images=images)
    assert [model_input(event.to_dict()) for event in events[: len(prefix)]] == prefix
    assert [window.anchor_id for window in current.windows] == [62, 58, 38, 19]
    assert [window.answer_id for window in current.windows] == [None, 60, 40, 20]
    assert [event.content for event in events if not isinstance(event, ModelRequest)]
    assert [
        part.data
        for event in events
        if isinstance(event, ModelRequest)
        for part in event.parts
        if isinstance(part, BinaryInput)
    ] == [b"old", b"new"]
    assert current.messages[-1].message_id == 62


@pytest.mark.parametrize(
    "origin", ["unmarked", "missing", "other_topic", "other_bot", "media"]
)
async def test_recursive_context_requires_own_answer_and_existing_same_topic_trigger(
    database: Database, origin: str
) -> None:
    store = MessageStore(database)
    instant = datetime(2026, 11, 7, tzinfo=UTC)
    if origin != "missing":
        await store.record(
            MemoryMessage(
                -123,
                10,
                "" if origin == "media" else "Earlier question",
                instant,
                42,
                "Anna",
                "Group",
                thread_id=8 if origin == "other_topic" else 7,
                media_type="animation" if origin == "media" else None,
            )
        )
    for number in range(30, 51):
        await store.record(
            MemoryMessage(
                -123,
                number,
                f"Message {number}",
                instant,
                (999 if origin == "other_bot" else 123) if number == 40 else 42,
                "Sender",
                "Group",
                thread_id=7,
                trigger_message_id=10 if number == 40 and origin != "unmarked" else None,
            )
        )
    dialogue = Dialogue(Agent[ReplyBatch]("unused"), history=store, bot_id=123)
    context = await dialogue.select_context((-123, 7), 50)
    assert [window.anchor_id for window in context.windows] == (
        [50, 10] if origin == "media" else [50]
    )
    assert context.unresolved_answer_ids == (
        () if origin in {"media", "other_bot"} else (40,)
    )
    assert all(
        message.chat_id == -123 and message.thread_id == 7 for message in context.messages
    )
    if origin == "media":
        assert (
            context.messages[0].text
            == "Please respond to this media in the context of our conversation."
        )
    else:
        assert [message.message_id for message in context.messages] == list(range(31, 51))
