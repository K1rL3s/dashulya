"""Isolate each integration test in a disposable PostgreSQL schema."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from dashulya.database import Database
from dashulya.database.migrate import migrate
from tests.postgres import require_database_url


@pytest.fixture
def schema() -> str:
    return "test_" + uuid4().hex


@pytest.fixture
def database_url(schema: str, monkeypatch: pytest.MonkeyPatch) -> str:
    url = make_conninfo(
        require_database_url(), options="-c search_path=" + schema + ",public"
    )
    monkeypatch.setenv("DATABASE_URL", url)
    return url


@pytest.fixture
async def database(schema: str, database_url: str) -> AsyncIterator[Database]:
    server_url = require_database_url()
    async with await AsyncConnection.connect(server_url, autocommit=True) as connection:
        await connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
        )
    try:
        # App-login tests inherit this disposable server's connection password.
        password = conninfo_to_dict(database_url).get("password")
        await migrate(
            database_url, app_password=str(password) if password is not None else None
        )
        database = Database(database_url)
        try:
            await database.open()
            yield database
        finally:
            await database.close()
    finally:
        async with await AsyncConnection.connect(
            server_url, autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
