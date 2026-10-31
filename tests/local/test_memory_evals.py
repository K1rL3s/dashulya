"""Verify eval contracts with synthetic originals and scripted AG2 responses."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from ag2.eval import InMemoryTraceSource, RunResult, Trace, TraceRef, evaluate_traces
from ag2.events import ModelMessage, ModelResponse, ToolCallEvent, ToolResultEvent
from ag2.middleware import Middleware
from ag2.testing import TestConfig
from dishka import make_async_container
from dishka_ag2 import AG2Provider, AG2Scope, DishkaAsyncMiddleware

from dashulya.ai.agents.factory import create_reply_agent
from dashulya.ai.message_format import MessageFormatter
from dashulya.ai.tools.history import chat_history
from dashulya.domain.memory import ChatMemoryService, MemoryAccess
from evals.suites.memory.__main__ import export, output_directory, summary
from evals.suites.memory.dataset import Dataset, instant, precedes
from evals.suites.memory.runtime import (
    EVAL_OWNER_ID,
    MemoryToolsProvider,
    capture,
    database_url,
)
from evals.suites.memory.scoring import (
    answer_text,
    canonical_count_matches,
    objective_scorer,
    semantic_scorer,
)


@pytest.fixture
def dataset(tmp_path: Path) -> Dataset:
    row = {
        "id": 7,
        "chat_key": "synthetic",
        "date": "2026-01-01T12:00:00+00:00",
        "text": "I use Alpha.",
        "user_id": 42,
        "user_name": "Alice",
        "user_username": "alice",
        "author": "Alice",
        "reply_to_msg_id": None,
    }
    cutoff = {"date": "2026-01-02T00:00:00+00:00", "message_id": 0, "exclusive": True}
    case = {
        "case_id": "A",
        "chat_key": "synthetic",
        "query": "What does Alice use?",
        "category": "people",
        "answerability": "answerable",
        "history_cutoff": cutoff,
        "split_group": "episode:A",
        "annotation_status": "draft",
        "partition": "development",
        "evidence_message_ids": [7],
        "gold": {
            "requirements": [
                {
                    "id": "r1",
                    "text": "Alice uses Alpha.",
                    "support_sets": [[7]],
                    "evidence_quotes": [{"message_id": 7, "quote": "I use Alpha."}],
                }
            ],
            "forbidden_claims": ["Alice uses Beta."],
        },
    }
    query = {key: case[key] for key in ("case_id", "chat_key", "query", "history_cutoff")}
    return Dataset(tmp_path, [case], {"A": query}, {7: row}, {"dataset_id": "synthetic"})


def test_gold_is_not_in_answerer_inputs_and_export_is_offline(
    dataset: Dataset, tmp_path: Path
) -> None:
    dataset.validate()
    task = next(iter(dataset.suite()))
    assert task.inputs == {"input": "What does Alice use?"}
    assert task.reference_outputs is not None
    assert task.reference_outputs["requirements"][0]["support_sets"] == [[7]]
    (dataset.path / "manifest.json").write_text("{}")
    target = tmp_path / "export"
    target.mkdir()
    export(dataset, None, target)
    query = json.loads((target / "queries.jsonl").read_text())
    assert "gold" not in query
    assert "answerability" not in query
    assert json.loads((target / "plan.json").read_text())["status"] == "prepared_not_run"


def test_validation_rejects_leaked_gold_and_future_support(dataset: Dataset) -> None:
    dataset.queries["A"]["gold"] = {}
    with pytest.raises(ValueError, match="evaluator-only"):
        dataset.validate()
    del dataset.queries["A"]["gold"]
    dataset.originals[7]["date"] = "2026-01-03T00:00:00+00:00"
    with pytest.raises(ValueError, match="authorized prefix"):
        dataset.validate()


def test_cutoff_uses_timezone_and_message_id(dataset: Dataset) -> None:
    row = dataset.originals[7]
    cutoff = {"date": "2026-01-01T15:00:00+03:00", "message_id": 8}
    assert precedes(row, cutoff)
    cutoff["message_id"] = 7
    assert not precedes(row, cutoff)
    cutoff["message_id"] = 6
    assert not precedes(row, cutoff)


def test_unknown_case_and_application_paths_rejected(
    dataset: Dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="existing case"):
        dataset.suite(["unknown"])
    with pytest.raises(ValueError, match="evals/runs"):
        output_directory(tmp_path / "var")
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/app")
    monkeypatch.setenv("EVAL_DATABASE_URL", "postgresql://other-user@localhost/app")
    with pytest.raises(ValueError, match="separate database"):
        database_url()


async def grade(
    dataset: Dataset,
    tmp_path: Path,
    *,
    retrieved: set[int],
    citation: int,
    support_sets: list[list[int]] | None = None,
) -> RunResult:
    if support_sets:
        dataset.cases[0]["gold"]["requirements"][0]["support_sets"] = support_sets
    call = ToolCallEvent("chat_history", arguments='{"action":"read"}')
    result = ToolResultEvent.from_call(
        call,
        {
            "message_blocks": [
                {"sources": {f"source:{n}": {"message_id": n} for n in retrieved}}
            ],
        },
    )
    trace = Trace(
        events=[
            call,
            result,
            ModelResponse(
                message=ModelMessage(
                    content=json.dumps({
                        "messages": [
                            {"text": "Alice uses Alpha.", "reply_to_message_id": None},
                            {
                                "text": f"https://t.me/synthetic/{citation}",
                                "reply_to_message_id": None,
                            },
                        ],
                    })
                )
            ),
        ],
        exception=None,
        duration_ms=1,
    )
    return await evaluate_traces(
        InMemoryTraceSource([(TraceRef(trace_id="a", task_id="A"), trace)]),
        suite=dataset.suite(),
        store_dir=tmp_path,
        scorers=[objective_scorer({"A": {7, 8}}, {"A": set()})],
    )


@pytest.mark.parametrize("field", ["content", "body"])
def test_answer_text_reads_all_messages_and_preserves_legacy_runs(field: str) -> None:
    for value, expected in (
        ({"messages": [{"text": "First"}, {"text": "Second"}]}, "First\n\nSecond"),
        ({"messages": []}, ""),
        ({"text": "Saved answer"}, "Saved answer"),
    ):
        outputs: dict[str, object] = {
            field: json.dumps(value) if field == "body" else value
        }
        assert answer_text(outputs) == expected


async def test_real_ag2_eval_records_citation_and_and_or_support_failures(
    dataset: Dataset, tmp_path: Path
) -> None:
    result = await grade(
        dataset, tmp_path, retrieved={7}, citation=8, support_sets=[[7, 8], [9]]
    )
    scores = {f.key: f.score for f in result.tasks[0].feedback}
    assert scores["citation_identity"] is False
    assert scores["annotated_support_coverage"] == 0
    assert scores["execution"] is True
    assert (tmp_path / f"{result.run_id}.json").is_file()


async def test_source_citation_pass_and_unannotated_alternative_not_gated(
    dataset: Dataset, tmp_path: Path
) -> None:
    result = await grade(dataset, tmp_path, retrieved={8}, citation=8)
    scores = {f.key: f.score for f in result.tasks[0].feedback}
    assert scores["citation_identity"] is True
    assert scores["annotated_support_coverage"] == 0
    assert summary(result)["slices"]["people"]["passed"] == 0  # No semantic grade.


async def test_capture_uses_injected_production_tool_and_fresh_stream(
    dataset: Dataset,
) -> None:
    service = AsyncMock(spec=ChatMemoryService)
    reader = AsyncMock()
    reader.query.return_value = {"action": "count", "count": 1, "coverage": {}}
    accesses: list[MemoryAccess] = []

    def history(access: MemoryAccess) -> AsyncMock:
        accesses.append(access)
        return reader

    service.history = history
    container = make_async_container(
        MemoryToolsProvider(),
        AG2Provider(),
        scopes=AG2Scope,
        context={
            ChatMemoryService: service,
            MessageFormatter: MessageFormatter("Europe/Moscow"),
        },
    )
    try:
        agent = create_reply_agent(
            TestConfig(
                ToolCallEvent("chat_history", arguments='{"action":"count"}'),
                '{"messages":[{"text":"One saved message.","reply_to_message_id":null}]}',
                '{"messages":[{"text":"Fresh answer.","reply_to_message_id":null}]}',
            ),
            tools=[chat_history],
            middleware=[Middleware(DishkaAsyncMiddleware, container=container)],
        )
        with patch.object(agent, "ask", wraps=agent.ask) as ask:
            first = await capture(agent, dataset.queries["A"])
            second = await capture(agent, dataset.queries["A"])
        assert (
            ask.call_args_list[0].kwargs["stream"]
            is not ask.call_args_list[1].kwargs["stream"]
        )
        assert first.exception is None
        assert second.exception is None
        assert reader.query.await_count == 2
        assert len(first.events_of(ToolResultEvent)) == 1
        assert len(second.events_of(ToolResultEvent)) == 1
        assert (
            accesses
            == [
                MemoryAccess(
                    EVAL_OWNER_ID,
                    is_private=True,
                    before=instant(dataset.queries["A"]["history_cutoff"]["date"]),
                )
            ]
            * 2
        )
    finally:
        await container.close()


async def test_judge_failure_is_unscored_and_cannot_pass(
    dataset: Dataset, tmp_path: Path
) -> None:
    result = await grade(dataset, tmp_path, retrieved={7}, citation=7)
    judge = semantic_scorer(TestConfig("invalid structured verdict"), {"A": {}})
    task = result.tasks[0].task
    feedback = await judge(
        inputs=task.inputs,
        outputs={"content": {"text": "Alpha."}},
        reference_outputs=task.reference_outputs,
        task=task,
        trace=result.tasks[0].trace,
    )
    assert feedback[0].score is None
    assert "provider details omitted" in (feedback[0].comment or "")


def test_count_requires_exact_canonical_predicate_and_successful_result() -> None:
    aggregation = {
        "author_id": 42,
        "after_inclusive": "2026-01-01T00:00:00+00:00",
        "before_exclusive": "2026-01-02T00:00:00+00:00",
        "text_contains_literal": "https://github.com/",
        "expected_count": 2,
    }
    args = {
        "action": "count",
        "author_id": 42,
        "after": "2026-01-01T03:00:00+03:00",
        "before": "2026-01-02T03:00:00+03:00",
        "text_contains": "https://github.com/",
    }

    def trace(arguments: dict[str, object], count: int) -> Trace:
        call = ToolCallEvent("chat_history", arguments=json.dumps(arguments))
        result = ToolResultEvent.from_call(call, {"count": count})
        return Trace(events=[call, result], exception=None, duration_ms=1)

    assert canonical_count_matches(trace(args, 2), aggregation)
    assert not canonical_count_matches(trace(args, 3), aggregation)
    assert not canonical_count_matches(trace({**args, "author_id": 43}, 2), aggregation)
    assert not canonical_count_matches(
        trace({**args, "name_contains": "Alice"}, 2), aggregation
    )
    assert not canonical_count_matches(trace({**args, "action": "list"}, 2), aggregation)


@pytest.mark.parametrize("required_fact_found", [True, False])
async def test_semantic_verdict_preserves_requirement_failure(
    dataset: Dataset,
    tmp_path: Path,
    required_fact_found: bool,
) -> None:
    result = await grade(dataset, tmp_path, retrieved={7}, citation=7)
    judge = semantic_scorer(
        TestConfig(
            json.dumps({
                "requirements": [
                    {"id": "r1", "satisfied": required_fact_found, "reason": "Fixture."}
                ],
                "forbidden_claims_absent": True,
                "additional_claims_grounded": True,
                "citations_support_claims": True,
                "abstention_correct": False,
                "coverage_qualified": True,
                "reason": "Fixture verdict.",
            })
        ),
        {"A": {}},
    )
    task = result.tasks[0].task
    feedback = await judge(
        inputs=task.inputs,
        outputs={"content": {"text": "Alpha."}},
        reference_outputs=task.reference_outputs,
        task=task,
        trace=result.tasks[0].trace,
    )
    assert feedback[0].score is required_fact_found
    assert feedback[1].key == "factual_answer"
    assert feedback[1].score is required_fact_found
