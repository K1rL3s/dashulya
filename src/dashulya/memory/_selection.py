"""Filter recall candidates in SQL and fetch only bounded supporting originals."""

from collections.abc import Sequence
from typing import Any

from psycopg import sql

from dashulya.domain.memory import MemoryInputError, OriginalMessage
from dashulya.memory._scope import VISIBLE_MESSAGES, scope_parameters
from dashulya.memory.history import CanonicalHistory, _date
from dashulya.memory.store import original


class RecallSelection:
    def __init__(self, history: CanonicalHistory) -> None:
        self.history = history
        self.database = history.store.database

    async def coverage(self, chat: str | None) -> dict[str, Any]:
        self.history._chat(chat)
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                sql.SQL("WITH visible AS NOT MATERIALIZED (")
                + VISIBLE_MESSAGES
                + sql.SQL("""
                ) SELECT collection AS chat,provenance,count(*) AS available_messages,
                    min(sent_at) AS first_saved_at,max(sent_at) AS last_saved_at,
                    count(*) FILTER (WHERE sent_at IS NULL) AS unknown_timestamps
                FROM visible GROUP BY collection,provenance ORDER BY collection,provenance
                """),
                scope_parameters(self.history.access, chat),
            )
            periods = await cursor.fetchall()
        if chat is not None and self.history.access.is_private and not periods:
            error_message = "Unknown saved chat collection within this access scope."
            raise MemoryInputError(error_message)
        first = [p["first_saved_at"] for p in periods if p["first_saved_at"] is not None]
        last = [p["last_saved_at"] for p in periods if p["last_saved_at"] is not None]
        return {
            **self.history.coverage([]),
            "available_messages": sum(p["available_messages"] for p in periods),
            "first_saved_at": min(first).isoformat() if first else None,
            "last_saved_at": max(last).isoformat() if last else None,
            "unknown_timestamps": sum(p["unknown_timestamps"] for p in periods),
            "known_periods": [
                {
                    key: value.isoformat()
                    if key in {"first_saved_at", "last_saved_at"} and value is not None
                    else value
                    for key, value in period.items()
                    if key != "unknown_timestamps"
                }
                for period in periods
            ],
        }

    async def originals(
        self,
        *,
        chat: str | None,
        author_id: int | None,
        after: str | None,
        before: str | None,
    ) -> tuple[list[OriginalMessage], frozenset[str]]:
        self.history._chat(chat)
        lower, upper = _date(after), _date(before)
        if lower is not None and upper is not None and lower > upper:
            error_message = "after must not be later than before."
            raise MemoryInputError(error_message)
        query = (
            sql.SQL("WITH RECURSIVE visible AS NOT MATERIALIZED (")
            + VISIBLE_MESSAGES
            + sql.SQL("""
        ), anchors AS MATERIALIZED (
            SELECT collection,message_id FROM visible
            WHERE (%(author_id)s::bigint IS NULL OR author_id=%(author_id)s)
            AND (%(after)s::timestamptz IS NULL OR sent_at>=%(after)s)
            AND (%(before)s::timestamptz IS NULL OR sent_at<=%(before)s)
        ), parents AS (
            SELECT m.collection,m.message_id,m.reply_to_id,m.sent_at,0 AS depth
            FROM visible m JOIN anchors a USING(collection,message_id)
            UNION ALL
            SELECT p.collection,p.message_id,p.reply_to_id,p.sent_at,c.depth+1
            FROM visible p JOIN parents c ON p.collection=c.collection
                AND p.message_id=c.reply_to_id
            WHERE c.depth<2 AND p.sent_at IS NOT NULL AND c.sent_at IS NOT NULL
                AND (p.sent_at,p.message_id)<=(c.sent_at,c.message_id)
                AND p.message_id<>c.message_id
        ), selected AS (
            SELECT DISTINCT collection,message_id FROM parents
        ) SELECT m.*,a.message_id IS NOT NULL AS is_primary FROM visible m
            JOIN selected s USING(collection,message_id)
            LEFT JOIN anchors a USING(collection,message_id)
        ORDER BY m.sent_at NULLS FIRST,m.collection,m.message_id
        """)
        )
        if author_id is None and lower is None and upper is None:
            query = (
                sql.SQL("WITH visible AS NOT MATERIALIZED (")
                + VISIBLE_MESSAGES
                + sql.SQL(
                    ") SELECT m.*,true AS is_primary FROM visible m ORDER BY m.sent_at NULLS FIRST,m.collection,m.message_id"
                )
            )
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                query,
                {
                    **scope_parameters(self.history.access, chat),
                    "author_id": author_id,
                    "after": lower,
                    "before": upper,
                },
            )
            rows = await cursor.fetchall()
        messages = [original(row) for row in rows]
        return messages, frozenset(
            message.source_id
            for message, row in zip(messages, rows, strict=True)
            if row["is_primary"]
        )

    async def context(
        self, targets: Sequence[OriginalMessage], *, chat: str | None
    ) -> list[OriginalMessage]:
        if not targets:
            return []
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                sql.SQL("WITH RECURSIVE visible AS NOT MATERIALIZED (")
                + VISIBLE_MESSAGES
                + sql.SQL("""
                ), targets AS (
                    SELECT m.* FROM visible m JOIN
                        unnest(%(collections)s::text[],%(ids)s::bigint[]) AS k(collection,message_id)
                        USING(collection,message_id)
                ), parents AS (
                    SELECT collection,message_id,reply_to_id,topic_id,sent_at,0 AS depth FROM targets
                    UNION ALL
                    SELECT p.collection,p.message_id,p.reply_to_id,p.topic_id,p.sent_at,c.depth+1
                    FROM visible p JOIN parents c ON p.collection=c.collection
                        AND p.topic_id IS NOT DISTINCT FROM c.topic_id
                        AND p.message_id=c.reply_to_id
                    WHERE c.depth<5 AND p.message_id<c.message_id
                        AND (coalesce(p.sent_at,'-infinity'),p.message_id)
                            <(coalesce(c.sent_at,'-infinity'),c.message_id)
                ), nearby AS (
                    SELECT n.collection,n.message_id FROM targets t CROSS JOIN LATERAL (
                        (SELECT p.collection,p.message_id FROM visible p
                        WHERE p.collection=t.collection AND p.topic_id IS NOT DISTINCT FROM t.topic_id
                            AND p.sent_at>=t.sent_at-interval '3 minutes'
                            AND (p.sent_at,p.message_id)<(t.sent_at,t.message_id)
                        ORDER BY p.sent_at DESC,p.message_id DESC LIMIT 2)
                        UNION ALL
                        (SELECT p.collection,p.message_id FROM visible p
                        WHERE p.collection=t.collection AND p.topic_id IS NOT DISTINCT FROM t.topic_id
                            AND p.sent_at<=t.sent_at+interval '3 minutes'
                            AND (p.sent_at,p.message_id)>(t.sent_at,t.message_id)
                        ORDER BY p.sent_at,p.message_id LIMIT 2)
                    ) n
                ), selected AS (
                    SELECT collection,message_id FROM parents WHERE depth>0
                    UNION SELECT collection,message_id FROM nearby
                ) SELECT m.* FROM visible m JOIN selected s USING(collection,message_id)
                ORDER BY m.sent_at NULLS FIRST,m.collection,m.message_id
                """),
                {
                    **scope_parameters(self.history.access, chat),
                    "collections": [m.collection for m in targets],
                    "ids": [m.message_id for m in targets],
                },
            )
            return [original(row) for row in await cursor.fetchall()]
