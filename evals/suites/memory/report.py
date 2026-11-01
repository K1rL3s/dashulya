"""Readable diagnostics; distinguish pipeline timing from model-quality results."""

import json
from typing import Any


def markdown(
    reports: list[dict[str, Any]], snapshot: dict[str, Any], *, mode: str
) -> str:
    lines = [
        "# History-summary replay",
        "",
        f"Mode: `{mode}`.",
        "",
        (
            "The boundary is Dialogue → AG2 → production memory tools → PostgreSQL. "
            "Telegram delivery is recorded only in the disposable database. "
            "Web, reactions and image downloads are excluded. Citation checks establish "
            "identity and exposure, not entailment or topic completeness. No human gold is supplied."
        ),
        "",
        (
            f"Snapshot preparation: {snapshot['duration_ms'] / 1000:.2f} s; "
            f"{snapshot['counts']['messages']:,} originals; "
            f"{snapshot['counts']['embeddings']:,} cached vectors. "
            "The source connection was read-only. Preparation made zero embedding API calls."
        ),
        "",
        "| Case | Total, s | Model calls | Tool calls | Peak input tokens | Initial → final serialized context, chars |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in reports:
        models = report["models"]
        tokens = report["evaluation"]["peak_prompt_tokens"]
        context = (
            f"{models[0]['input_chars']:,} → {models[-1]['input_chars']:,}"
            if models
            else "—"
        )
        lines.append(
            f"| {report['case']['case_id']} | {report['duration_ms'] / 1000:.2f} | {len(models)} | {len(report['tools'])} | {tokens if tokens is not None else 'not measured'} | {context} |"
        )
    lines += [
        "",
        (
            "Serialized context sizes include AG2 event metadata and provider replay state. "
            "They exclude system prompts/tool schemas, which are saved separately per call. "
            "They are not token estimates. Tool wall time merges overlapping calls; "
            "nested search-stage durations must not be added to their parent retrieval duration."
        ),
    ]
    for report in reports:
        lines += [
            "",
            f"## {report['case']['case_id']}",
            "",
            f"Error: {report['error'] or 'none'}.",
            "",
        ]
        failed = [
            name
            for name, value in report["evaluation"]["checks"].items()
            if value is False
        ]
        unknown = [
            name
            for name, value in report["evaluation"]["checks"].items()
            if value is None
        ]
        lines += [
            "Failed checks: " + (", ".join(failed) or "none") + ".",
            "Unscored checks: " + (", ".join(unknown) or "none") + ".",
            "",
            "| Tool | Start, ms | Duration, ms | Outcome |",
            "| --- | ---: | ---: | --- |",
        ]
        for tool in report["tools"]:
            lines.append(
                f"| {tool['name']} | {tool['start_ms']} | {tool['duration_ms']} | {tool['outcome']} |"
            )
        lines += ["", "Search-stage metrics:", "", "```json"]
        lines += [
            json.dumps(
                [
                    {"tool_call_id": event.get("tool_call_id"), **event["search_metrics"]}
                    for event in report["logs"]
                    if event.get("message") == "Memory search completed"
                ],
                indent=2,
            ),
            "```",
            "",
            f"Full contexts, tool schemas, arguments and results: `{report['case']['case_id']}.json`.",
        ]
    return "\n".join(lines) + "\n"
