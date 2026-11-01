import json
from datetime import UTC, datetime

from ag2 import Agent, MemoryStream
from ag2.events import ToolCallEvent, ToolResultEvent
from ag2.testing import TestConfig

from dashulya.ai.message_format import external_input
from evals.suites.memory.diagnostics import Diagnostics
from evals.suites.memory.report import markdown
from evals.suites.memory.scenario import (
    Scenario,
    ScriptedReplayConfig,
    clean_json,
    interval_ms,
    score,
)


def test_overlapping_tools_are_not_double_counted() -> None:
    assert (
        interval_ms([
            {"start_ms": 20, "duration_ms": 100},
            {"start_ms": 30, "duration_ms": 50},
            {"start_ms": 150, "duration_ms": 10},
        ])
        == 110
    )


def test_trace_detects_unbounded_recall_and_context_budget() -> None:
    case = Scenario(
        case_id="day",
        session="private",
        query="Summarize yesterday",
        chat_id=42,
        user_id=42,
        target_chat_id=-1001,
        sent_at="2026-01-02T10:00:00+00:00",
        after="2026-01-01T00:00:00+00:00",
        before="2026-01-02T00:00:00+00:00",
        max_prompt_tokens=500,
    )
    trace = Diagnostics(
        models=[{"prompt_tokens": 501}],
        tools=[
            {
                "tool_call_id": "x",
                "name": "search_memory",
                "arguments": {"query": "yesterday"},
                "outcome": "ok",
            }
        ],
    )
    result = score(case, trace, "Some answer", {}, None)
    assert result["unbounded_recall_calls"] == ["x"]
    assert result["checks"]["context_token_budget"] is False
    assert result["checks"]["recall_filters_present"] is False
    trace.tools[0]["arguments"].update(
        after="2026-01-01T00:00:00+00:00", before="2026-01-02T00:00:00+00:00"
    )
    trace.models[0]["prompt_tokens"] = 400
    result = score(case, trace, "Some answer", {}, None)
    assert result["checks"]["context_token_budget"] is True
    assert result["checks"]["recall_filters_present"] is True
    trace.models[0]["prompt_tokens"] = None
    result = score(case, trace, "Some answer", {}, None)
    assert result["checks"]["context_token_budget"] is None
    assert result["peak_prompt_tokens"] is None


def test_saved_context_and_nested_logs_redact_configured_credentials() -> None:
    value = {
        "models": [
            {"prompt": ["fixture-key"], "events": [{"content": "text with fixture-key"}]}
        ],
        "logs": [{"arguments": {"sql": ["SELECT 'fixture-key'"]}}],
    }
    result = clean_json(value, ["fixture-key"])
    assert "fixture-key" not in str(result)
    assert result["models"][0]["prompt"] == ["<REDACTED>"]


async def test_scripted_cases_do_not_reset_to_the_first_case_in_a_reused_agent() -> None:
    config = ScriptedReplayConfig({
        "day": TestConfig("day answer"),
        "week": TestConfig("week answer"),
    })
    agent = Agent("replay", config=config)
    stream = MemoryStream()
    config.select("day")
    assert await (await agent.ask("Day?", stream=stream)).content() == "day answer"
    config.select("week")
    assert await (await agent.ask("Week?", stream=stream)).content() == "week answer"


def test_readable_report_does_not_invent_token_usage_for_scripted_runs() -> None:
    report = markdown(
        [
            {
                "case": {"case_id": "day"},
                "models": [{"input_chars": 100}, {"input_chars": 300}],
                "tools": [],
                "logs": [],
                "error": None,
                "duration_ms": 50,
                "evaluation": {
                    "peak_prompt_tokens": None,
                    "checks": {"context_token_budget": None},
                },
            }
        ],
        {
            "duration_ms": 100,
            "counts": {"messages": 2, "embeddings": 2},
        },
        mode="scripted_pipeline_with_synthetic_query_vectors",
    )
    assert "not measured" in report
    assert "100 → 300" in report
    assert "Unscored checks: context_token_budget" in report
    assert "not entailment" in report


def test_scope_check_uses_message_id_to_break_equal_timestamp_ties() -> None:
    instant = datetime(2026, 1, 2, 10, tzinfo=UTC)
    case = Scenario(
        case_id="tie",
        session="private",
        query="History?",
        chat_id=42,
        user_id=42,
        target_chat_id=-1001,
        sent_at=instant,
        after=instant.replace(day=1),
        before=instant,
    )
    diagnostics = Diagnostics(initial_sources={"source:fixture:1"}, request_message_id=2)
    sources = {
        "source:fixture:1": {
            "chat_id": 42,
            "message_id": 1,
            "sent_at": instant,
            "author_id": 42,
            "url": None,
        }
    }
    assert score(case, diagnostics, "Answer", sources, None)["checks"][
        "retrieved_source_scope"
    ]
    diagnostics.request_message_id = 1
    assert not score(case, diagnostics, "Answer", sources, None)["checks"][
        "retrieved_source_scope"
    ]


def test_seen_sources_include_urls_from_compact_sql_rows() -> None:
    instant = datetime(2026, 1, 2, 10, tzinfo=UTC)
    case = Scenario(
        case_id="table",
        session="private",
        query="History?",
        chat_id=42,
        user_id=42,
        target_chat_id=-1001,
        sent_at=instant,
        after=instant.replace(day=1),
        before=instant,
    )
    url = "https://t.me/example/1"
    event = ToolResultEvent.from_call(
        ToolCallEvent(
            "chat_history",
            arguments=json.dumps({"sql": ["SELECT message_id,url FROM messages"]}),
        ),
        external_input(
            json.dumps({
                "results": [{"columns": ["message_id", "url"], "rows": [[1, url]]}]
            }),
            "memory",
        ),
    )
    diagnostics = Diagnostics(results=[event])
    sources = {
        "source:fixture:1": {
            "chat_id": -1001,
            "message_id": 1,
            "sent_at": instant.replace(day=1),
            "author_id": 42,
            "url": url,
        }
    }
    result = score(case, diagnostics, url, sources, None)
    assert result["seen_source_count"] == 1
    assert result["checks"]["citations_seen_by_agent"] is True
