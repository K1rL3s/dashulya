import asyncio

import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo

from dashulya.database import Database
from dashulya.memory.lexical import LexicalDocument, LexicalIndex


def documents(*texts: str, collection: str = "telegram:-123") -> list[LexicalDocument]:
    return [LexicalDocument(collection, text) for text in texts]


async def search(
    index: LexicalIndex, rows: list[LexicalDocument], query: str
) -> list[str]:
    return await index.search(rows, [row.key for row in rows], [query], limit=160)


async def test_long_tokens_remain_searchable(database: Database) -> None:
    index = LexicalIndex(database)
    rows = documents("prefix" + "x" * 12000, "prefix prefix", "unrelated")
    assert set(await search(index, rows, "prefix")) == {rows[0].key, rows[1].key}


async def test_native_bm25_prefers_short_direct_match_and_filters_before_limit(
    database: Database,
) -> None:
    index = LexicalIndex(database)
    rows = documents("needle " + "noise " * 100, "needle", "unrelated")
    ranked = await search(index, rows, "needle")
    assert ranked == [rows[1].key, rows[0].key]
    assert await index.search(rows, [rows[0].key], ["needle"], limit=1) == [rows[0].key]
    assert (
        await index.search(rows, [r.key for r in rows], ["needle", "needle"], limit=2)
        == ranked
    )


async def test_other_chats_and_cached_versions_do_not_change_statistics(
    database: Database,
) -> None:
    index = LexicalIndex(database)
    rows = documents("redis", "redis postgres", "postgres postgres", "other")
    before = await search(index, rows, "postgres")
    foreign = documents(
        *(f"postgres secret {n}" for n in range(1001)), collection="telegram:-999"
    )
    await index.prepare(foreign)
    await index.prepare(documents("future postgres", "later edit"))
    assert await search(index, rows, "postgres") == before
    async with database.connection() as connection:
        configs = await (
            await connection.execute("SELECT namespace FROM lexical_cache_config")
        ).fetchone()
        assert configs is not None
        entry = await (
            await connection.execute(
                "SELECT table_name FROM lexical_snapshots WHERE collection=%s ORDER BY used_at DESC LIMIT 1",
                (rows[0].collection,),
            )
        ).fetchone()
        assert entry is not None
        with pytest.raises(errors.CheckViolation):
            await connection.execute(
                sql.SQL(
                    "INSERT INTO {} VALUES (99999,'foreign','telegram:-999','secret')"
                ).format(sql.Identifier(configs["namespace"], entry["table_name"]))
            )


async def test_snapshot_survives_a_new_index_instance_and_eviction_is_bounded(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dashulya.memory.lexical._CACHE_SIZE", 2)
    rows = documents("original needle")
    index = LexicalIndex(database)
    assert await search(index, rows, "needle") == [rows[0].key]
    restarted = LexicalIndex(database)
    assert await search(restarted, rows, "needle") == [rows[0].key]
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS n FROM lexical_snapshots")
        ).fetchone()
        assert row == {"n": 1}
    for n in range(4):
        await restarted.prepare(documents(f"version {n}"))
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS n FROM lexical_snapshots")
        ).fetchone()
        assert row == {"n": 2}
    assert await search(index, rows, "needle") == [rows[0].key]


async def test_concurrent_instances_share_one_complete_snapshot(
    database: Database,
) -> None:
    rows = documents("needle", "unrelated")
    found = await asyncio.wait_for(
        asyncio.gather(*[
            search(LexicalIndex(database), rows, "needle") for _ in range(4)
        ]),
        timeout=5,
    )
    assert found == [[rows[0].key]] * 4
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS n FROM lexical_snapshots")
        ).fetchone()
        assert row == {"n": 1}


async def test_runtime_role_reuses_admin_preparation_and_builds_new_snapshots(
    database: Database,
    database_url: str,
) -> None:
    rows = documents("admin prepared needle")
    await LexicalIndex(database).prepare(rows)
    runtime = Database(make_conninfo(database_url, user="dashulya_app"))
    try:
        await runtime.open()
        index = LexicalIndex(runtime)
        assert await search(index, rows, "needle") == [rows[0].key]
        fresh = documents("runtime prepared needle")
        assert await search(index, fresh, "needle") == [fresh[0].key]
        async with runtime.connection() as connection:
            with pytest.raises(errors.InsufficientPrivilege):
                await connection.execute(
                    "UPDATE lexical_cache_config SET namespace='public'"
                )
    finally:
        await runtime.close()


async def test_cancelled_build_rolls_back_index_and_journal(database: Database) -> None:
    async with database.connection() as connection:
        await connection.execute(
            "CREATE FUNCTION pause_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(30); RETURN NEW; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER pause_snapshot BEFORE INSERT ON lexical_snapshots FOR EACH ROW EXECUTE FUNCTION pause_snapshot()"
        )
    task = asyncio.create_task(
        LexicalIndex(database).prepare(documents("cancelled needle"))
    )
    try:
        async with asyncio.timeout(5):
            while True:
                async with database.connection() as connection:
                    row = await (
                        await connection.execute(
                            "SELECT count(*) AS n FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND state='active' AND query LIKE 'INSERT INTO lexical_snapshots%%'"
                        )
                    ).fetchone()
                if row and row["n"]:
                    break
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with database.connection() as connection:
        row = await (
            await connection.execute("SELECT count(*) AS n FROM lexical_snapshots")
        ).fetchone()
        assert row == {"n": 0}
        row = await (
            await connection.execute(
                "SELECT count(*) AS n FROM pg_tables WHERE schemaname=(SELECT namespace FROM lexical_cache_config)"
            )
        ).fetchone()
        assert row == {"n": 0}
        await connection.execute("DROP TRIGGER pause_snapshot ON lexical_snapshots")
    rows = documents("recovered needle")
    assert await search(LexicalIndex(database), rows, "needle") == [rows[0].key]


async def test_unicode_prefixes_preserve_normalization(
    database: Database, subtests: pytest.Subtests
) -> None:
    index = LexicalIndex(database)
    rows = documents(
        "naïve CAFÉ cafe\u0301 Straße STRASSE",
        "инструменты инструментами",
        "project🤯 tools🤷 привет\u0301 ΙΣΟΣ ὈΔΥΣΣΕΎΣ",
        "\u0301start separate 👩\u200d💻 combining\u036f",
        "edge\ud7ff tail \U0010ffff maximal \U0010ffffmore",
        "edge\ue000 next scalar \U0010fffftail",
        "carry\U0010ffffvalue",
        "carrz other",
    )
    for prefix, positions in [
        ("café", [0]),
        ("naïve", [0]),
        ("strasse", [0]),
        ("инструмент", [1]),
        ("project", [2]),
        ("привет", [2]),
        ("edge\ud7ff", [4]),
        ("\U0010ffff", [4, 5]),
        ("carry\U0010ffff", [6]),
    ]:
        with subtests.test(prefix=prefix):
            assert set(await search(index, rows, prefix)) == {
                rows[n].key for n in positions
            }
