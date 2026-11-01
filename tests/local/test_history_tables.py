import json
from copy import deepcopy

from dashulya.ai.tools.history import _table_result
from evals.suites.memory.scoring import history_rows


def test_compact_tables_preserve_evidence_and_batch_positions() -> None:
    original = {
        "query_count": 3,
        "results": [
            {
                "query_index": 1,
                "outcome": "ok",
                "columns": ["source_id", "message_id", "text", "url", "metadata"],
                "rows": [
                    {
                        "source_id": f"source:0123456789abcdef01234567:{i}",
                        "message_id": i,
                        "text": "Длинный текст | перенос\n" * 30,
                        "url": None,
                        "metadata": {"tags": ["quoted", "文"]},
                    }
                    for i in range(10)
                ],
                "row_count": 10,
                "truncated": True,
                "image": {"status": "already_supplied"},
            },
            {"query_index": 2, "outcome": "error", "error": "Unknown column"},
            {
                "query_index": 3,
                "outcome": "ok",
                "columns": ["n"],
                "rows": [{"n": 0}],
                "row_count": 1,
                "truncated": False,
            },
        ],
    }
    before = deepcopy(original)
    compact = json.dumps(
        _table_result(original), ensure_ascii=False, separators=(",", ":")
    )
    assert history_rows(json.loads(compact)) == original == before
    assert compact.count('"source_id"') == 1
    assert len(compact) < len(json.dumps(original, ensure_ascii=False))
