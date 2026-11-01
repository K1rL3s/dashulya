"""Canonical SQL visibility shared by original reads and recall selection."""

from typing import Any

from psycopg import sql

from dashulya.domain.memory import MemoryAccess

VISIBLE_MESSAGES = sql.SQL("""SELECT * FROM messages WHERE
    (%(private)s OR chat_id=%(chat_id)s)
    AND (%(chat)s::text IS NULL OR collection=%(chat)s)
    AND (%(cutoff)s::timestamptz IS NULL OR (
        (edited_at IS NULL OR edited_at<%(cutoff)s)
        AND CASE WHEN %(cutoff_id)s::bigint IS NULL OR chat_id IS DISTINCT FROM %(chat_id)s
            THEN sent_at<%(cutoff)s
            ELSE (sent_at,message_id)<(%(cutoff)s,%(cutoff_id)s) END
    ))""")


def scope_parameters(access: MemoryAccess, chat: str | None) -> dict[str, Any]:
    return {
        "private": access.is_private,
        "chat_id": access.chat_id,
        "chat": chat,
        "cutoff": access.before,
        "cutoff_id": access.before_id,
    }
