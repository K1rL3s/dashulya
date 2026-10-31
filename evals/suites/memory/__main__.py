"""Validate/export offline; only the explicit run command contacts model APIs."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ag2.config import OpenAIResponsesConfig
from ag2.eval import InMemoryTraceSource, RunResult, TraceRef, evaluate_traces
from dotenv import load_dotenv

from dashulya.ai.agents.models import create_models

from .dataset import Dataset
from .runtime import capture, database_url, memory_agent
from .scoring import JUDGE_PROMPT, objective_scorer, semantic_scorer


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def output_directory(path: Path | None) -> Path:
    root = Path("evals/runs").resolve()
    target = path.resolve() if path else root / ("memory-" + uuid4().hex)
    if not target.is_relative_to(root) or target == root:
        msg = "Write evaluation artifacts to a new directory under evals/runs/."
        raise ValueError(msg)
    target.mkdir(parents=True, exist_ok=False)
    return target


def export(dataset: Dataset, ids: list[str] | None, target: Path) -> None:
    suite = dataset.suite(ids)
    with (
        (target / "queries.jsonl").open("w") as queries,
        (target / "review.jsonl").open("w") as review,
    ):
        for task in suite:
            queries.write(
                json.dumps(dataset.queries[task.task_id], ensure_ascii=False) + "\n"
            )
            review.write(
                json.dumps(
                    {
                        "case_id": task.task_id,
                        "query": task.inputs["input"],
                        "reference": task.reference_outputs,
                        "metadata": task.metadata,
                        "human_label": None,
                        "human_notes": "",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    write_json(
        target / "plan.json",
        {
            "dataset": suite.name,
            "case_ids": [task.task_id for task in suite],
            "categories": dict(Counter(task.metadata["category"] for task in suite)),
            "source_manifest_sha256": hashlib.sha256(
                (dataset.path / "manifest.json").read_bytes()
            ).hexdigest(),
            "status": "prepared_not_run",
            "judge_status": "uncalibrated",
        },
    )


def summary(result: RunResult) -> dict[str, Any]:
    slices: dict[str, list[bool]] = {}
    errors = 0
    for item in result.tasks:
        scores = {feedback.key: feedback.score for feedback in item.feedback}
        required = {
            "execution",
            "citation_identity",
            "retrieval_scope",
            "semantic_quality",
        }
        if item.task.metadata["category"] == "counts":
            required.add("canonical_count")
        complete = item.trace.exception is None and all(
            scores.get(key) is True for key in required
        )
        errors += item.trace.exception is not None or any(
            scores.get(key) is None for key in required
        )
        for key in (item.task.metadata["category"], item.task.metadata["answerability"]):
            slices.setdefault(key, []).append(complete)
    return {
        "status": "scored",
        "judge_status": "uncalibrated_provisional",
        "cases": len(result.tasks),
        "execution_or_scoring_errors": errors,
        "slices": {
            key: {
                "passed": sum(values),
                "total": len(values),
                "pass_rate": sum(values) / len(values),
            }
            for key, values in sorted(slices.items())
        },
        "note": "Development results only. Human calibration and held-out validation are pending.",
    }


async def run(
    dataset: Dataset, ids: list[str] | None, target: Path, *, model: str, judge_model: str
) -> None:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or not model or not judge_model:
        msg = "Set OPENAI_API_KEY and supply both --model and --judge-model (or their environment variables)."
        raise ValueError(msg)
    url = database_url()
    suite = dataset.suite(ids)
    cases = {case["case_id"]: case for case in dataset.cases}
    export(dataset, ids, target)
    write_json(
        target / "runtime.json",
        {
            "started_at": datetime.now(UTC).isoformat(),
            "ag2_version": importlib.metadata.version("ag2"),
            "model": model,
            "judge_model": judge_model,
            "judge_prompt_sha256": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
            "memory": "production hybrid BM25 + text-embedding-3-small/512",
            "scope": "single unbound archive, owner-private; no Telegram group identity asserted",
            "case_ids": [task.task_id for task in suite],
        },
    )
    config = create_models(api_key=key, reply_model=model, decision_model=model).reply
    groups: dict[str, list[str]] = {}
    for task in suite:
        groups.setdefault(
            json.dumps(task.metadata["history_cutoff"], sort_keys=True), []
        ).append(task.task_id)
    pairs = []
    visible_ids: dict[str, set[int]] = {}
    context_ids: dict[str, set[int]] = {}
    contexts: dict[str, dict[str, Any]] = {}
    for group in groups.values():
        async with memory_agent(
            dataset, cases[group[0]], config, api_key=key, url=url
        ) as agent:
            for case_id in group:
                query = dataset.queries[case_id]
                visible_ids[case_id] = {
                    row["id"] for row in dataset.visible(cases[case_id])
                }
                context = query.get("input_context", {})
                contexts[case_id] = context
                context_ids[case_id] = {
                    row["id"]
                    for row in [
                        *context.get("preceding_messages", []),
                        *context.get("reply_ancestors", []),
                    ]
                }
                trace = await capture(agent, query)
                pairs.append((TraceRef(trace_id=uuid4().hex, task_id=case_id), trace))
        # Each cutoff gets its own index; future originals never affect retrieval.
    result = await evaluate_traces(
        InMemoryTraceSource(pairs),
        suite=suite,
        store_dir=target,
        run_id="ag2-results",
        concurrency=1,
        on_missing_task="error",
        scorers=[
            objective_scorer(visible_ids, context_ids),
            semantic_scorer(
                OpenAIResponsesConfig(
                    model=judge_model,
                    api_key=key,
                    timeout=60,
                    max_retries=1,
                ),
                contexts,
            ),
        ],
    )
    write_json(target / "summary.json", summary(result))


def command(args: argparse.Namespace) -> None:
    dataset = Dataset.load(args.dataset)
    suite = dataset.suite(args.ids)
    if args.command == "validate":
        sys.stdout.write(f"Validated {len(suite)} source-backed cases; no model calls.\n")
        return
    target = output_directory(args.out)
    if args.command == "export":
        export(dataset, args.ids, target)
    else:
        asyncio.run(
            run(dataset, args.ids, target, model=args.model, judge_model=args.judge_model)
        )
    sys.stdout.write(f"Artifacts: {target}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "export", "run"))
    parser.add_argument(
        "--dataset", type=Path, default=Path("evals/datasets/ostrikov/v2")
    )
    parser.add_argument(
        "--case", action="append", dest="ids", help="Repeat to select case IDs."
    )
    parser.add_argument("--out", type=Path, help="New directory under evals/runs/.")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", ""))
    parser.add_argument("--judge-model", default=os.getenv("OPENAI_EVAL_JUDGE_MODEL", ""))
    args = parser.parse_args()
    try:
        command(args)
    except Exception:
        # Database DSNs and provider exceptions must never reach terminal output.
        sys.stderr.write(
            "Memory eval command failed. Check dataset, selection, output path and required environment settings.\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    load_dotenv(override=False)
    main()
