"""Test database validation and helpers for seeding canonical history."""

import os
import sys
from collections.abc import Sequence
from dataclasses import replace

from dashulya.domain.memory import MemoryAccess, OriginalMessage
from dashulya.memory.history import CanonicalHistory
from dashulya.memory.store import MessageStore


def require_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not url:
        error = (
            "Set TEST_DATABASE_URL to an isolated pgvector-enabled test database "
            "for the full suite, or run just test-local without PostgreSQL."
        )
        raise RuntimeError(error)
    return url


async def seed_history(
    store: MessageStore,
    access: MemoryAccess,
    *,
    imported: Sequence[OriginalMessage],
    import_chat_id: int | None = None,
) -> CanonicalHistory:
    for row in imported:
        await store.put(
            replace(row, collection=f"telegram:{import_chat_id}", chat_id=import_chat_id)
            if import_chat_id is not None
            else row
        )
    return CanonicalHistory(store, access)


if __name__ == "__main__":
    try:
        require_database_url()
    except RuntimeError as error:
        sys.exit(str(error))
