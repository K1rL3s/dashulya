"""Read selected evidence through the model-facing tool and real scoped storage."""

import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

from ag2 import Agent, MemoryStream
from ag2.events import TextInput, ToolCallEvent, ToolResultEvent
from ag2.testing import TestConfig

from dashulya.database import Database
from dashulya.domain.memory import MemoryAccess, MemoryMessage, message_source_id
from dashulya.domain.messages import Reply, ReplyBatch
from dashulya.main import create_container
from dashulya.main.config import Settings
from dashulya.memory.history import CanonicalHistory
from dashulya.memory.store import MessageStore
from tests.support import (
    TEST_API_KEY,
    MemoryOverride,
    ModelOverrides,
    input_payload,
    reply_json,
)

NOW = datetime(2026, 11, 8, 10, tzinfo=UTC)
GROUP = -100123


async def test_source_batches_page_threads_and_do_not_repeat_supplied_text(
    database: Database, cleanup: AsyncExitStack
) -> None:
    store = MessageStore(database)
    for mid, chat, text, parent, topic, instant in [
        (1, GROUP, "Root 日本語", None, 7, NOW - timedelta(hours=1)),
        (2, GROUP, "First reply", 1, 7, NOW - timedelta(minutes=50)),
        (3, GROUP, "Second reply", 2, 7, NOW - timedelta(minutes=40)),
        (4, GROUP, "OTHER TOPIC", 1, 8, NOW - timedelta(minutes=30)),
        (5, -100456, "FOREIGN SECRET", None, 7, NOW - timedelta(hours=1)),
        (6, GROUP, "FUTURE SECRET", 1, 7, NOW + timedelta(minutes=1)),
    ]:
        await store.record(
            MemoryMessage(
                chat,
                mid,
                text,
                instant,
                42,
                "Author",
                "Group",
                thread_id=topic,
                reply_to_id=parent,
            )
        )
    root = message_source_id(f"telegram:{GROUP}", 1)
    await store.record(
        MemoryMessage(
            GROUP,
            7,
            "LATER EDIT SECRET",
            NOW - timedelta(hours=1),
            42,
            "Author",
            "Group",
            edited_at=NOW + timedelta(minutes=1),
        )
    )
    foreign = message_source_id("telegram:-100456", 5)
    future = message_source_id(f"telegram:{GROUP}", 6)
    edited = message_source_id(f"telegram:{GROUP}", 7)

    def read(**options: object) -> ToolCallEvent:
        return ToolCallEvent(
            "read_sources",
            arguments=json.dumps({
                "source_ids": [root, foreign, future, edited],
                "include_replies": True,
                "limit": 2,
                **options,
            }),
        )

    reader = CanonicalHistory(store, MemoryAccess(GROUP, False, before=NOW, before_id=99))
    container = create_container(
        Settings(bot_token="123:TEST", openai_api_key=TEST_API_KEY, owner_user_id=42),
        MemoryOverride(reader=reader),
        ModelOverrides(
            reply=TestConfig(read(), read(), read(offset=2), reply_json("Read")),
            decision=TestConfig("false"),
        ),
    )
    cleanup.push_async_callback(container.close)
    agent = await container.get(Agent[ReplyBatch])
    stream = MemoryStream()
    result = await agent.ask(
        "Read evidence",
        stream=stream,
        dependencies={
            "memory_access": MemoryAccess(GROUP, False, before=NOW, before_id=99),
        },
    )
    assert await result.content() == ReplyBatch((Reply("Read"),))
    events = [
        e for e in await stream.history.get_events() if isinstance(e, ToolResultEvent)
    ]
    assert len(events) == 3, "read_sources must be registered and return evidence"
    data = []
    for event in events:
        part = event.result.parts[0]
        assert isinstance(part, TextInput)
        data.append(json.loads(input_payload(part.content)))
    assert [item["status"] for item in data[0]["results"]] == [
        "ok",
        "not_found",
        "not_found",
        "not_found",
    ]
    assert data[0]["results"][0]["next_offset"] == 2
    assert "Root 日本語" in json.dumps(data[0], ensure_ascii=False)
    assert "First reply" in json.dumps(data[0])
    assert "Root 日本語" not in json.dumps(data[1], ensure_ascii=False)
    assert data[1]["already_supplied"] == [
        root,
        message_source_id(f"telegram:{GROUP}", 2),
    ]
    assert "Second reply" in json.dumps(data[2])
    assert data[2]["results"][0]["next_offset"] is None
    assert "SECRET" not in json.dumps(data)
    assert "OTHER TOPIC" not in json.dumps(data)


async def test_source_text_continuation_and_new_turn_keep_evidence_available(
    database: Database, cleanup: AsyncExitStack
) -> None:
    store = MessageStore(database)
    await store.record(
        MemoryMessage(
            GROUP,
            1,
            "HEADtailEND",
            NOW - timedelta(hours=1),
            42,
            "Author",
            "Group",
        )
    )
    source = message_source_id(f"telegram:{GROUP}", 1)
    access = MemoryAccess(GROUP, False, before=NOW, before_id=99)

    def read(**options: object) -> ToolCallEvent:
        return ToolCallEvent(
            "read_sources",
            arguments=json.dumps({
                "source_ids": [source],
                "text_chars": 4,
                **options,
            }),
        )

    container = create_container(
        Settings(bot_token="123:TEST", openai_api_key=TEST_API_KEY, owner_user_id=42),
        MemoryOverride(reader=CanonicalHistory(store, access)),
        ModelOverrides(
            reply=TestConfig(
                read(),
                read(text_offset=4),
                read(text_chars=11),
                read(text_chars=11),
                reply_json("Read"),
                read(),
                reply_json("Read again"),
            ),
            decision=TestConfig("false"),
        ),
    )
    cleanup.push_async_callback(container.close)
    agent = await container.get(Agent[ReplyBatch])
    stream = MemoryStream()
    for _ in range(2):
        result = await agent.ask(
            "Read", stream=stream, dependencies={"memory_access": access}
        )
        await result.content()
    data = []
    for event in await stream.history.get_events():
        if isinstance(event, ToolResultEvent):
            part = event.result.parts[0]
            assert isinstance(part, TextInput)
            data.append(json.loads(input_payload(part.content)))
    assert "HEAD" in data[0]["message_blocks"][0]["text"]
    assert data[0]["message_blocks"][0]["sources"][source]["next_offset"] == 4
    assert "tail" in data[1]["message_blocks"][0]["text"]
    assert "HEADtailEND" in data[2]["message_blocks"][0]["text"]
    assert data[3]["already_supplied"] == [source]
    assert "HEAD" in data[4]["message_blocks"][0]["text"]
    assert data[4]["already_supplied"] == []
