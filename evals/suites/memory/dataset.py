"""Keep private originals, answerer inputs and evaluator references separate."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from ag2.eval import Suite, Task


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        msg = "Dataset timestamps must include a timezone."
        raise ValueError(msg)
    return result


def precedes(row: dict[str, Any], cutoff: dict[str, Any]) -> bool:
    return (instant(row["date"]), row["id"]) < (
        instant(cutoff["date"]),
        cutoff["message_id"],
    )


@dataclass(frozen=True)
class Dataset:
    path: Path
    cases: list[dict[str, Any]]
    queries: dict[str, dict[str, Any]]
    originals: dict[int, dict[str, Any]]
    manifest: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load the v2 contract and verify immutable input hashes before use."""
        manifest = json.loads((path / "manifest.json").read_text())
        baseline = (path / manifest["baseline"]).resolve()
        for root, entries in (
            (path, manifest["artifacts"]),
            (baseline, manifest["baseline_files"]),
        ):
            for name, entry in entries.items():
                expected = entry["sha256"] if isinstance(entry, dict) else entry
                if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
                    msg = f"Dataset hash mismatch: {name}"
                    raise ValueError(msg)
        cases = read_jsonl(path / "cases.jsonl")
        queries = read_jsonl(path / "queries.jsonl")
        originals = read_jsonl(baseline / "messages.jsonl")
        result = cls(
            path,
            cases,
            {q["case_id"]: q for q in queries},
            {row["id"]: row for row in originals},
            manifest,
        )
        if len(result.queries) != len(queries) or len(result.originals) != len(originals):
            msg = "Duplicate query or original identity."
            raise ValueError(msg)
        result.validate()
        return result

    def visible(self, case: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            row
            for row in self.originals.values()
            if row["chat_key"] == case["chat_key"]
            and precedes(row, case["history_cutoff"])
        ]

    def validate(self) -> None:
        ids = [c["case_id"] for c in self.cases]
        if len(set(ids)) != len(ids) or set(ids) != set(self.queries):
            msg = "Cases and input-only queries must have matching unique IDs."
            raise ValueError(msg)
        for case in self.cases:
            query = self.queries[case["case_id"]]
            allowed = {
                "case_id",
                "chat_key",
                "language",
                "query",
                "history_cutoff",
                "memory_scope",
                "input_context",
                "original_request_id",
            }
            if set(query) - allowed:
                msg = "Unexpected answerer fields; gold must stay evaluator-only."
                raise ValueError(msg)
            for field in ("query", "chat_key", "history_cutoff"):
                if query[field] != case[field]:
                    msg = f"Case/query mismatch: {case['case_id']} {field}"
                    raise ValueError(msg)
            if case["history_cutoff"]["exclusive"] is not True:
                msg = "Only exclusive historical cutoffs are supported."
                raise ValueError(msg)
            visible = {row["id"]: row for row in self.visible(case)}
            for requirement in case["gold"]["requirements"]:
                for support in requirement["support_sets"]:
                    if not support or any(number not in visible for number in support):
                        msg = "Gold support must exist inside the authorized prefix."
                        raise ValueError(msg)
                for quote in requirement["evidence_quotes"]:
                    if quote["quote"] not in visible[quote["message_id"]]["text"]:
                        msg = "Gold quotation differs from the canonical original."
                        raise ValueError(msg)
            aggregation = case["gold"].get("aggregation")
            if aggregation:
                matched = {
                    row["id"]
                    for row in visible.values()
                    if row["user_id"] == aggregation["author_id"]
                    and instant(aggregation["after_inclusive"])
                    <= instant(row["date"])
                    < instant(aggregation["before_exclusive"])
                    and (aggregation["text_contains_literal"] or "").casefold()
                    in (row["text"] or "").casefold()
                }
                if (
                    matched != set(aggregation["matching_message_ids"])
                    or len(matched) != aggregation["expected_count"]
                ):
                    msg = "Count reference differs from canonical history."
                    raise ValueError(msg)
            context = query.get("input_context", {})
            for row in [
                *context.get("preceding_messages", []),
                *context.get("reply_ancestors", []),
            ]:
                if row["id"] not in visible or row["text"] != visible[row["id"]]["text"]:
                    msg = "Input context must contain unchanged pre-cutoff originals."
                    raise ValueError(msg)

    def suite(self, case_ids: list[str] | None = None) -> Suite:
        selected = [c for c in self.cases if case_ids is None or c["case_id"] in case_ids]
        if not selected or (
            case_ids and set(case_ids) != {c["case_id"] for c in selected}
        ):
            msg = "Select at least one existing case ID."
            raise ValueError(msg)
        tasks = [
            Task(
                task_id=case["case_id"],
                inputs={"input": case["query"]},
                reference_outputs={
                    **case["gold"],
                    "originals": [
                        self.originals[n] for n in case["evidence_message_ids"]
                    ],
                },
                tags=(case["category"], case["answerability"], case["partition"]),
                metadata={
                    key: case[key]
                    for key in (
                        "chat_key",
                        "category",
                        "answerability",
                        "history_cutoff",
                        "split_group",
                        "annotation_status",
                    )
                },
            )
            for case in selected
        ]
        return Suite(
            tasks=tuple(tasks), name=self.manifest["dataset_id"], source=str(self.path)
        )
