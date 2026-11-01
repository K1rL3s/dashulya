"""Administrative entry point for fresh setup and existing database upgrades."""

import asyncio
import os
import sys

from dotenv import load_dotenv

from dashulya.database.migrate import migrate


def main() -> None:
    load_dotenv(override=False)
    url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    if not url:
        msg = "Set MIGRATION_DATABASE_URL to an administrative PostgreSQL URL."
        raise SystemExit(msg)
    try:
        applied = asyncio.run(
            migrate(url, app_password=os.getenv("POSTGRES_APP_PASSWORD"))
        )
    except Exception as error:
        raise SystemExit(
            "Database migration failed (" + type(error).__name__ + ")."
        ) from None
    sys.stdout.write(
        "Database migrations complete: "
        + (", ".join(applied) or "already current")
        + "\n"
    )


if __name__ == "__main__":
    main()
