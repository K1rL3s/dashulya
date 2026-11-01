import argparse
from datetime import UTC, datetime

import pytest

from evals.suites.bm25.corpus import Case
from evals.suites.bm25.tune import (
    Profile,
    Question,
    build_query,
    run,
    split_questions,
    support_metrics,
)


def question(name: str, group: str, sources: tuple[str, ...]) -> Question:
    return Question(
        Case(name, "query", datetime(2026, 1, 1, tzinfo=UTC), ("archive:chat",)),
        group,
        sources,
    )


def test_split_keeps_transitive_episodes_and_shared_sources_together() -> None:
    questions = [
        question("q0", "a", ("source:1",)),
        question("q1", "a", ("source:2",)),
        question("q2", "b", ("source:2",)),
        question("q3", "c", ("source:3",)),
        question("q4", "d", ("source:4",)),
    ]
    tuning, validation = split_questions(questions)
    tune_ids = {q.case.case_id for q in tuning}
    valid_ids = {q.case.case_id for q in validation}
    assert tune_ids.isdisjoint(valid_ids)
    assert tune_ids | valid_ids == {q.case.case_id for q in questions}
    assert {"q0", "q1", "q2"} <= tune_ids or {"q0", "q1", "q2"} <= valid_ids
    assert {key for q in tuning for key in q.evidence_ids}.isdisjoint(
        key for q in validation for key in q.evidence_ids
    )
    reversed_split = split_questions(list(reversed(questions)))
    assert {q.case.case_id for q in reversed_split[0]} == tune_ids
    assert {q.case.case_id for q in reversed_split[1]} == valid_ids


def test_support_requires_complete_and_set_and_accepts_parent_and_or_alternative() -> (
    None
):
    case = Case(
        "support",
        "query",
        datetime(2026, 1, 1, tzinfo=UTC),
        requirements=(
            (("parent", "anchor"),),
            (("absent",), ("alternative",)),
            (("parent", "missing"),),
        ),
    )
    metrics = support_metrics(case, [("parent", "anchor"), ("other",), ("alternative",)])
    assert metrics["requirements"] == 3
    assert metrics["covered_8"] == 2
    assert metrics["covered_32"] == 2
    assert metrics["reciprocal_rank_sum"] == pytest.approx(1 + 1 / 3)


def test_zero_weight_removes_parent_only_and_author_only_match_arms() -> None:
    case = question("q", "g", ()).case
    result = build_query(
        "chat_index",
        case,
        Profile("split_prefix", parents=0, authors=0),
        {"body": ["query"], "parents": ["query"], "authors": ["query"]},
    )
    assert result is not None
    query, parameters = result
    text = query.as_string()
    assert '"body" @@@' in text
    assert '"parents"' not in text
    assert '"authors"' not in text
    assert parameters[0] == ["query"]


def test_stemmed_fields_analyze_full_words_instead_of_raw_prefix_arrays() -> None:
    case = Case("stem", "Какие модели работали?", datetime(2026, 1, 1, tzinfo=UTC))
    result = build_query(
        "chat_index",
        case,
        Profile("split_stem", parents=0.25, authors=0),
        {"body": [], "parents": [], "authors": []},
    )
    assert result is not None
    query, parameters = result
    assert '"body" |||' in query.as_string()
    assert '"parents" |||' in query.as_string()
    assert "match_disjunction" not in query.as_string()
    assert parameters[:2] == ["модели работали", "модели работали"]


def test_tuning_refuses_application_database_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BM25_DATABASE_URL", "postgresql://localhost/dashulya")
    with pytest.raises(ValueError, match="disposable bm25_eval"):
        run(argparse.Namespace())
