"""Exercise model SQL with real PostgreSQL permissions and trusted conversation scope."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from dashulya.database import Database
from dashulya.database.migrate import migrate
from dashulya.domain.memory import MemoryAccess, MemoryInputError, MemoryMessage
from dashulya.memory.sql_history import SqlHistory
from dashulya.memory.store import MessageStore
from dashulya.observability import JsonLogFormatter

GROUP = -100123
OTHER = -100456
NOW = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture
async def reader(database: Database, database_url: str) -> AsyncIterator[SqlHistory]:
    store = MessageStore(database)
    for chat, message, text, author, parent in [
        (GROUP, 1, "deployment root", 42, None),
        (GROUP, 2, "deployment reply", 77, 1),
        (GROUP, 3, "deployment sibling", 42, 1),
        (GROUP, 4, "unrelated", 77, None),
        (OTHER, 1, "OTHER CHAT SECRET", 42, None),
    ]:
        await store.record(
            MemoryMessage(
                chat,
                message,
                text,
                NOW + timedelta(seconds=message),
                author,
                "Participant",
                "Group",
                reply_to_id=parent,
            )
        )
    app = Database(make_conninfo(database_url, user="dashulya_app"))
    try:
        await app.open()
        yield SqlHistory(app, MemoryAccess(GROUP, False))
    finally:
        await app.close()


async def test_queries_execute_as_restricted_role(reader: SqlHistory) -> None:
    result = (
        await reader.query([
            "SELECT current_user AS role, session_user AS login, count(*) AS saved FROM messages"
        ])
    )["results"][0]
    assert result["rows"] == [
        {"role": "dashulya_history_reader", "login": "dashulya_app", "saved": 4}
    ]


async def test_excerpt_of_compressed_unicode_original(
    database: Database, reader: SqlHistory
) -> None:
    # PostgreSQL 16.12 misvalidated partial TOAST slices at UTF-8 boundaries.
    await MessageStore(database).record(
        MemoryMessage(GROUP, 5, "日向" * 2000, NOW, 42, "Author", "Group")
    )
    result = (
        await reader.query([
            (
                "SELECT left(text,1) AS first, substring(text from 2 for 1) AS second "
                "FROM messages WHERE message_id=5"
            )
        ])
    )["results"][0]
    assert result["outcome"] == "ok"
    assert result["rows"] == [{"first": "日", "second": "向"}]


async def test_grouping_failure_explains_the_repair_and_preserves_batch(
    reader: SqlHistory,
) -> None:
    result = await reader.query([
        (
            "SELECT m.message_id,m.author,count(r.message_id) AS replies FROM messages m "
            "LEFT JOIN messages r ON r.chat=m.chat AND r.reply_to_message_id=m.message_id "
            "GROUP BY m.chat,m.message_id"
        ),
        (
            "WITH replies AS (SELECT chat,reply_to_message_id,count(*) AS n FROM messages "
            "GROUP BY chat,reply_to_message_id) SELECT m.message_id,m.author,coalesce(r.n,0) AS replies "
            "FROM messages m LEFT JOIN replies r ON r.chat=m.chat AND r.reply_to_message_id=m.message_id "
            "WHERE m.reply_to_message_id IS NULL ORDER BY m.message_id"
        ),
    ])
    assert result["results"][0]["outcome"] == "error"
    assert "GROUP BY" in result["results"][0]["error"]
    assert "CTE" in result["results"][0]["error"]
    assert result["results"][1]["rows"] == [
        {"message_id": 1, "author": "Participant", "replies": 2},
        {"message_id": 4, "author": "Participant", "replies": 0},
    ]


async def test_scope_applies_before_ctes_unions_and_aggregates(
    reader: SqlHistory,
) -> None:
    result = (
        await reader.query([
            "WITH all_rows AS (SELECT * FROM messages WHERE true OR chat_id = -100456) SELECT chat_id, count(*) AS saved FROM all_rows GROUP BY chat_id UNION ALL SELECT chat_id, count(*) FROM pg_temp.messages WHERE chat_id = -100456 GROUP BY chat_id"
        ])
    )["results"][0]
    assert result["rows"] == [{"chat_id": GROUP, "saved": 4}]
    assert "SECRET" not in str(result)


async def test_range_counts_authors_and_thread(reader: SqlHistory) -> None:
    assert (
        (
            await reader.query([
                "SELECT message_id FROM messages WHERE message_id BETWEEN 2 AND 3 ORDER BY message_id"
            ])
        )["results"][0]
    )["rows"] == [{"message_id": 2}, {"message_id": 3}]
    assert (
        (
            await reader.query([
                "SELECT count(*) AS n FROM messages WHERE text ILIKE '%deployment%' AND sent_at >= TIMESTAMPTZ '2026-11-01 00:00:00+00'"
            ])
        )["results"][0]
    )["rows"] == [{"n": 3}]
    assert (
        (
            await reader.query([
                "SELECT message_id FROM messages WHERE author_id = 42 ORDER BY message_id"
            ])
        )["results"][0]
    )["rows"] == [{"message_id": 1}, {"message_id": 3}]
    thread = (
        await reader.query([
            "WITH RECURSIVE thread AS (SELECT * FROM messages WHERE message_id = 2 UNION SELECT m.* FROM messages m JOIN thread t ON m.chat = t.chat AND m.topic_id IS NOT DISTINCT FROM t.topic_id AND ((m.message_id = t.reply_to_message_id AND m.message_id < t.message_id) OR (m.reply_to_message_id = t.message_id AND m.message_id > t.message_id))) SELECT message_id FROM thread ORDER BY message_id"
        ])
    )["results"][0]
    assert thread["rows"] == [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]


async def test_fixed_cutoff_hides_request_future_and_late_edits(
    database: Database, reader: SqlHistory
) -> None:
    store = MessageStore(database)
    await store.record(
        MemoryMessage(
            GROUP, 5, "future", NOW + timedelta(seconds=10), 42, "Author", "Group"
        )
    )
    await store.record(
        MemoryMessage(
            GROUP,
            1,
            "late edit",
            NOW + timedelta(seconds=1),
            42,
            "Author",
            "Group",
            edited_at=NOW + timedelta(seconds=11),
        )
    )
    scoped = SqlHistory(
        database,
        MemoryAccess(GROUP, False, before=NOW + timedelta(seconds=3), before_id=3),
    )
    assert (
        (await scoped.query(["SELECT message_id FROM messages ORDER BY message_id"]))[
            "results"
        ][0]
    )["rows"] == [{"message_id": 2}]


async def test_reader_has_no_base_table_write_or_setting_privileges(
    database_url: str, database: Database, schema: str
) -> None:
    del database
    for statement in [
        sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema, "messages")),
        sql.SQL("DELETE FROM {}").format(sql.Identifier(schema, "messages")),
        sql.SQL("SELECT pg_catalog.set_config('role','none',true)"),
        sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema, "embeddings")),
        sql.SQL("SELECT * FROM {}").format(
            sql.Identifier(schema, "lexical_cache_config")
        ),
        sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema, "lexical_snapshots")),
    ]:
        async with (
            await AsyncConnection.connect(
                make_conninfo(database_url, user="dashulya_app"), autocommit=True
            ) as connection,
            connection.transaction(),
        ):
            await connection.execute("SET LOCAL ROLE dashulya_history_reader")
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(statement.as_string())


async def test_result_limit_and_sql_error_recovery(reader: SqlHistory) -> None:
    result = (await reader.query(["SELECT generate_series(1,1000) AS n"]))["results"][0]
    assert result["row_count"] == 50
    assert result["truncated"] is True
    failed = (await reader.query(["SELECT private_missing_column FROM messages"]))[
        "results"
    ][0]
    assert failed["outcome"] == "error"
    assert "Unknown column" in failed["error"]
    assert ((await reader.query(["SELECT count(*) AS n FROM messages"]))["results"][0])[
        "rows"
    ] == [{"n": 4}]


async def test_cancelled_query_releases_role_and_temporary_view(
    reader: SqlHistory, database_url: str
) -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            (
                await reader.query([
                    "WITH RECURSIVE spin(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM spin) SELECT sum(n) FROM spin"
                ])
            )["results"][0]
    assert ((await reader.query(["SELECT count(*) AS n FROM messages"]))["results"][0])[
        "rows"
    ] == [{"n": 4}]
    async with await AsyncConnection.connect(
        database_url, autocommit=True, row_factory=dict_row
    ) as connection:
        row = await (
            await connection.execute(
                "SELECT count(*) AS leaked FROM pg_class WHERE relname = 'messages' AND relnamespace IN (SELECT oid FROM pg_namespace WHERE nspname LIKE 'pg_temp_%')"
            )
        ).fetchone()
        assert row == {"leaked": 0}


async def test_migrations_repeat_without_reapplying_and_runtime_cannot_migrate(
    database: Database, database_url: str
) -> None:
    del database
    assert await migrate(database_url) == []
    with pytest.raises(errors.InsufficientPrivilege):
        await migrate(make_conninfo(database_url, user="dashulya_app"))


async def test_modified_migration_journal_blocks_runtime_and_migrator(
    database: Database, database_url: str
) -> None:
    async with database.connection() as connection:
        await connection.execute("UPDATE schema_migrations SET checksum='invalid'")
    app = Database(make_conninfo(database_url, user="dashulya_app"))
    try:
        with pytest.raises(RuntimeError, match="migrations are required"):
            await app.open()
        assert app.pool.closed
        with pytest.raises(RuntimeError, match="migration was modified"):
            await migrate(database_url)
    finally:
        await app.close()


async def test_batch_keeps_order_and_recovers_from_individual_failures(
    reader: SqlHistory,
) -> None:
    result = await reader.query([
        "SELECT count(*) AS saved FROM messages",
        "SELECT missing_column FROM messages",
        "SELECT * FROM public.messages",
        "SELECT FROM",
        "SELECT current_user AS role, count(*) AS saved FROM messages WHERE author_id = 42",
    ])
    assert result["query_count"] == 5
    assert [item["query_index"] for item in result["results"]] == [1, 2, 3, 4, 5]
    assert [item["outcome"] for item in result["results"]] == [
        "ok",
        "error",
        "error",
        "error",
        "ok",
    ]
    assert result["results"][0]["rows"] == [{"saved": 4}]
    assert result["results"][4]["rows"] == [
        {"role": "dashulya_history_reader", "saved": 2}
    ]
    assert "Unknown column" in result["results"][1]["error"]
    assert "SECRET" not in str(result)


async def test_batch_shares_result_budget(reader: SqlHistory) -> None:
    large = "SELECT '" + "x" * 4000 + "' AS body FROM generate_series(1,20)"
    result = await reader.query([large, large, "SELECT count(*) AS saved FROM messages"])
    used = sum(
        len(json.dumps(row, ensure_ascii=False))
        for item in result["results"]
        for row in item.get("rows", [])
    )
    assert used <= 48_000
    assert result["results"][0]["truncated"] is True
    assert result["results"][1]["truncated"] is True
    assert result["results"][2]["rows"] == [{"saved": 4}]


@pytest.mark.parametrize("size", [0, 6])
async def test_batch_size_is_bounded(reader: SqlHistory, size: int) -> None:
    with pytest.raises(MemoryInputError, match="1-5"):
        await reader.query(["SELECT 1"] * size)


async def test_timed_out_query_does_not_discard_other_batch_results(
    reader: SqlHistory,
) -> None:
    timeout = asyncio.timeout
    with patch(
        "dashulya.memory.sql_history.asyncio.timeout",
        side_effect=lambda seconds: timeout(min(seconds, 0.1)),
    ):
        result = await reader.query([
            "SELECT count(*) AS saved FROM messages",
            "WITH RECURSIVE spin(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM spin) SELECT sum(n) FROM spin",
            "SELECT count(*) AS saved FROM messages",
        ])
    assert [item["outcome"] for item in result["results"]] == ["ok", "error", "ok"]
    assert result["results"][0]["rows"] == result["results"][2]["rows"] == [{"saved": 4}]
    assert "deadline" in result["results"][1]["error"]


async def test_each_batch_query_is_logged_without_selected_rows(
    reader: SqlHistory, caplog: pytest.LogCaptureFixture
) -> None:
    queries = [
        "SELECT text FROM messages ORDER BY message_id",
        "SELECT missing_column FROM messages",
    ]
    with caplog.at_level("INFO", logger="dashulya.memory.sql_history"):
        await reader.query(queries)
    records = [
        json.loads(JsonLogFormatter().format(record))
        for record in caplog.records
        if record.message == "History SQL query completed"
    ]
    assert [record["sql"] for record in records] == queries
    assert [record["query_index"] for record in records] == [1, 2]
    assert [record["outcome"] for record in records] == ["ok", "error"]
    assert len({record["batch_id"] for record in records}) == 1
    assert all(
        record["query_count"] == 2
        and record["chat_id"] == GROUP
        and record["duration_ms"] >= 0
        for record in records
    )
    assert records[0]["row_count"] == 4
    assert "deployment root" not in json.dumps(records)
