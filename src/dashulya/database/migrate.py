"""Versioned PostgreSQL setup, run with an administrative connection."""

import hashlib
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).with_name("migrations")
HISTORY_ROLE = "dashulya_history_reader"
APP_USER = "dashulya_app"


async def require_schema(connection: AsyncConnection[dict[str, Any]]) -> None:
    cursor = await connection.execute(
        "SELECT to_regclass('schema_migrations') AS journal"
    )
    row = await cursor.fetchone()
    if row and row["journal"]:
        cursor = await connection.execute("SELECT name, checksum FROM schema_migrations")
        applied = {row["name"]: row["checksum"] for row in await cursor.fetchall()}
        if all(
            applied.get(path.name) == hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(MIGRATIONS.glob("*.sql"))
        ):
            return
    msg = "Database migrations are required: run python -m dashulya.main.migrations."
    raise RuntimeError(msg)


async def migrate(url: str, *, app_password: str | None = None) -> list[str]:
    applied = []
    async with (
        await AsyncConnection.connect(
            url, autocommit=True, row_factory=dict_row
        ) as connection,
        connection.transaction(),
    ):
        await connection.execute("SELECT pg_advisory_xact_lock(489627301)")
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        cursor = await connection.execute("SELECT name, checksum FROM schema_migrations")
        known = {row["name"]: row["checksum"] for row in await cursor.fetchall()}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            content = path.read_bytes()
            checksum = hashlib.sha256(content).hexdigest()
            if path.name in known:
                if known[path.name] != checksum:
                    raise RuntimeError(
                        "An applied database migration was modified: " + path.name
                    )
                continue
            await connection.execute(content.decode())
            await connection.execute(
                "INSERT INTO schema_migrations(name,checksum) VALUES (%s,%s)",
                (path.name, checksum),
            )
            applied.append(path.name)
        if app_password is not None:
            if not app_password:
                msg = "The application database password must not be empty."
                raise ValueError(msg)
            await connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(APP_USER), sql.Literal(app_password)
                )
            )
    return applied
