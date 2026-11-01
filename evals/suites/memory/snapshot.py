"""Copy canonical originals and existing search caches into an isolated eval schema."""

from datetime import datetime
from time import perf_counter
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from dashulya.database import Database

TABLES = ("collections", "messages", "embeddings")


async def copy_snapshot(
    source_url: str, target: Database, *, before: datetime
) -> dict[str, Any]:
    """Read one consistent source snapshot; never create embeddings or change it."""
    options = str(conninfo_to_dict(source_url).get("options") or "")
    source_url = make_conninfo(
        source_url, options=options + " -c default_transaction_read_only=on"
    )
    started = perf_counter()
    counts = {}
    async with (
        await AsyncConnection.connect(
            source_url, autocommit=True, row_factory=dict_row
        ) as source,
        source.transaction(),
        target.connection() as destination,
        destination.transaction(),
    ):
        await source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table in TABLES:
            cursor = await destination.execute(
                sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
            )
            row = await cursor.fetchone()
            if row is None or row["n"]:
                error_message = (
                    "Snapshot destination must contain empty evaluation tables."
                )
                raise ValueError(error_message)
        for table in TABLES:
            select = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
            if table == "messages":
                select += sql.SQL(
                    " WHERE sent_at<{} AND (edited_at IS NULL OR edited_at<{})"
                ).format(sql.Literal(before), sql.Literal(before))
            async with (
                source.cursor().copy(
                    sql.SQL("COPY ({}) TO STDOUT (FORMAT BINARY)").format(select)
                ) as reader,
                destination.cursor().copy(
                    sql.SQL("COPY {} FROM STDIN (FORMAT BINARY)").format(
                        sql.Identifier(table)
                    )
                ) as writer,
            ):
                async for chunk in reader:
                    await writer.write(chunk)
            # Bulk COPY into a new schema has no planner statistics yet. Match
            # a maintained runtime database before measuring query latency.
            await destination.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
            cursor = await destination.execute(
                sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
            )
            row = await cursor.fetchone()
            assert row is not None
            counts[table] = row["n"]
    return {
        "duration_ms": round((perf_counter() - started) * 1000),
        "before_exclusive": before.isoformat(),
        "counts": counts,
        "source_read_only": True,
        "embedding_provider_calls": 0,
    }
