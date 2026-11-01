"""Exercise extension installation instead of inheriting the image's template."""

from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from dashulya.database import Database
from dashulya.database.migrate import migrate
from dashulya.memory.lexical import LexicalDocument, LexicalIndex
from tests.postgres import require_database_url


async def test_empty_database_installs_search_in_its_required_schema() -> None:
    server_url = require_database_url()
    name = "test_bootstrap_" + uuid4().hex
    async with await AsyncConnection.connect(server_url, autocommit=True) as admin:
        await admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
        )
        try:
            url = make_conninfo(server_url, dbname=name)
            await migrate(url)
            database = Database(make_conninfo(url, user="dashulya_app"))
            try:
                await database.open()
                async with database.connection() as connection:
                    row = await (
                        await connection.execute(
                            "SELECT extversion,extnamespace::regnamespace::text AS namespace FROM pg_extension WHERE extname='pg_search'"
                        )
                    ).fetchone()
                    assert row == {"extversion": "0.26.4", "namespace": "paradedb"}
                document = LexicalDocument("telegram:-123", "native bootstrap needle")
                assert await LexicalIndex(database).search(
                    [document], [document.key], ["needle"], limit=1
                ) == [document.key]
            finally:
                await database.close()
        finally:
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
