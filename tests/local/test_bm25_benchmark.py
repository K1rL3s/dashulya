from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from dashulya.domain.memory import OriginalMessage
from evals.suites.bm25.corpus import Case, documents, expand


def message(number: int, text: str, **changes: Any) -> OriginalMessage:
    value = OriginalMessage(
        collection="archive:allowed",
        chat_id=None,
        message_id=number,
        text=text,
        sent_at=datetime(2026, 1, number, tzinfo=UTC),
        author_id=10,
        author="Alice",
        chat_title="Allowed",
        chat_username=None,
        topic_id=None,
        reply_to_id=None,
        edited_at=None,
        media_type=None,
        url=None,
        provenance="import",
    )
    return replace(value, **changes)


def test_filtered_primary_keeps_authorized_parent_but_excludes_other_scope_and_late_edit() -> (
    None
):
    originals = [
        message(1, "parent evidence", author_id=20, reply_to_id=9),
        message(2, "current evidence", reply_to_id=1),
        message(3, "future evidence"),
        message(2, "foreign evidence", collection="archive:hidden"),
        message(
            1,
            "late edit",
            edited_at="2026-01-04T00:00:00+00:00",
            message_id=9,
            sent_at=datetime(2025, 12, 31, tzinfo=UTC),
        ),
    ]
    case = Case(
        "scope",
        "evidence",
        datetime(2026, 1, 3, tzinfo=UTC),
        collections=("archive:allowed",),
        after=datetime(2026, 1, 2, tzinfo=UTC),
        author_id=10,
        requirements=((("archive:allowed/1", "archive:allowed/2"),),),
    )
    found = documents(originals, case)
    assert len(found) == 1
    assert found[0].sources == ("archive:allowed/1", "archive:allowed/2")
    assert "parent evidence" in found[0].text
    assert all(text not in found[0].text for text in ["future", "foreign", "late edit"])


def test_prefix_expansion_preserves_full_unicode_membership_without_duplicates() -> None:
    vocabulary = sorted(["redis", "redirection", "редис", "редиска", "other"])
    assert expand(["red", "redis", "реди"], vocabulary) == [
        "redirection",
        "redis",
        "редис",
        "редиска",
    ]
    assert expand(["missing"], vocabulary) == []
