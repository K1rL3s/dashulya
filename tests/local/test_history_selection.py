from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from dashulya.domain.memory import MemoryAccess, MemoryInputError, OriginalMessage
from dashulya.memory.history import CanonicalHistory
from dashulya.memory.store import MessageStore

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def original(number: int, parent: int | None = None, **changes: Any) -> OriginalMessage:
    return replace(
        OriginalMessage(
            collection="telegram:-123",
            chat_id=-123,
            message_id=number,
            text=f"Message {number}",
            sent_at=NOW + timedelta(seconds=number),
            author_id=42,
            author="Owner",
            chat_title="Group",
            chat_username=None,
            topic_id=None,
            reply_to_id=parent,
            edited_at=None,
            media_type=None,
            url=None,
            provenance="observed",
        ),
        **changes,
    )


def reader(messages: list[OriginalMessage]) -> CanonicalHistory:
    store = AsyncMock(spec=MessageStore)
    store.originals.return_value = messages
    return CanonicalHistory(store, MemoryAccess(-123, False))


async def test_message_id_range_is_inclusive_and_cursor_keeps_its_filters() -> None:
    history = reader([original(number) for number in range(1, 10)])
    first = await history.query(
        min_message_id=3, max_message_id=7, limit=2, parent_hops=0
    )
    assert [item["message_id"] for item in first["messages"]] == [6, 7]
    assert first["matching_count"] == 5
    second = await history.query(
        min_message_id=3,
        max_message_id=7,
        limit=2,
        parent_hops=0,
        cursor=first["next_cursor"],
    )
    assert [item["message_id"] for item in second["messages"]] == [4, 5]
    with pytest.raises(MemoryInputError, match="cursor"):
        await history.query(
            min_message_id=4, max_message_id=7, cursor=first["next_cursor"]
        )


async def test_thread_from_a_reply_includes_root_and_all_reply_branches_only() -> None:
    history = reader([
        original(1),
        original(2, 1),
        original(3),
        original(4, 2),
        original(5, 1),
        original(6, 5),
        original(7, 3),
    ])
    page = await history.query(thread_message_id=4, limit=50, parent_hops=0)
    assert [item["message_id"] for item in page["messages"]] == [1, 2, 4, 5, 6]
    assert page["thread"]["root_message_id"] == 1
    assert page["thread"]["root_known"] is True
    assert page["matching_count"] == 5


async def test_thread_pagination_preserves_siblings_and_root() -> None:
    history = reader([original(1), *[original(number, 1) for number in range(2, 8)]])
    cursor = None
    found: list[int] = []
    for _ in range(4):
        page = await history.query(
            thread_message_id=3, limit=2, parent_hops=0, cursor=cursor
        )
        found.extend(item["message_id"] for item in page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(found) == list(range(1, 8))
    assert len(found) == len(set(found))


async def test_missing_thread_parent_is_reported_and_other_topics_stay_out() -> None:
    history = reader([
        original(3, 2, topic_id=7),
        original(4, 3, topic_id=7),
        original(5, 3, topic_id=8),
    ])
    page = await history.query(thread_message_id=4, parent_hops=0)
    assert [item["message_id"] for item in page["messages"]] == [3, 4]
    assert page["thread"]["root_known"] is False
    assert (await history.query(thread_message_id=99))["messages"] == []


async def test_single_item_list_preserves_long_original_continuation() -> None:
    target = original(7, text="Я" * 15000)
    history = reader([target])
    first = await history.query(source_id=target.source_id, limit=1, parent_hops=0)
    assert len(first["messages"][0]["text"]) == 12000
    second = await history.query(
        source_id=target.source_id, limit=1, text_offset=12000, parent_hops=0
    )
    assert first["messages"][0]["text"] + second["messages"][0]["text"] == target.text
    assert second["messages"][0]["next_offset"] is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"min_message_id": 9, "max_message_id": 3},
        {"min_message_id": 0},
        {"thread_message_id": -1},
        {"text_offset": 1},
    ],
)
async def test_invalid_selection_arguments_are_rejected(
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(MemoryInputError):
        await reader([]).query(**arguments)
