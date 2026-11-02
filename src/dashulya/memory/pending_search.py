"""Bounded SQL-only lexical recall of revisions awaiting background indexing."""

from typing import Any

from psycopg import sql

from dashulya.database import Database
from dashulya.domain.memory import MemoryAccess
from dashulya.memory._scope import VISIBLE_MESSAGES, scope_parameters


class PendingSearch:
    def __init__(self, database: Database, access: MemoryAccess) -> None:
        self.database = database
        self.access = access

    async def search(
        self,
        prefixes: list[str],
        *,
        chat: str | None,
        author_id: int | None,
        after: object,
        before: object,
    ) -> tuple[list[dict[str, Any]], int]:
        parameters = {
            **scope_parameters(self.access, chat),
            "author": author_id,
            "after": after,
            "before": before,
            "query": " | ".join(
                "'" + prefix.replace("'", "''") + "':*" for prefix in prefixes
            ),
        }
        visible = (
            sql.SQL("WITH RECURSIVE visible AS NOT MATERIALIZED (")
            + VISIBLE_MESSAGES
            + sql.SQL(
                "), eligible AS (SELECT m.collection,m.message_id,q.version FROM visible m "
                "JOIN search_pending q USING(collection,message_id) "
                "WHERE (%(author)s::bigint IS NULL OR m.author_id=%(author)s) "
                "AND (%(after)s::timestamptz IS NULL OR m.sent_at>=%(after)s) "
                "AND (%(before)s::timestamptz IS NULL OR m.sent_at<=%(before)s)) "
            )
        )
        async with self.database.connection() as connection:
            row = await (
                await connection.execute(
                    visible + sql.SQL("SELECT count(*) AS total FROM eligible"),
                    parameters,
                )
            ).fetchone()
            assert row is not None
            total = int(row["total"])
            if not total or not prefixes:
                return [], total
            cursor = await connection.execute(
                visible
                + sql.SQL(""",
                pending AS (SELECT * FROM eligible ORDER BY version DESC LIMIT 128),
                lineage AS (
                    SELECT m.collection,m.message_id AS anchor,m.message_id,m.reply_to_id,m.sent_at,m.body,0 AS depth
                    FROM visible m JOIN pending p USING(collection,message_id)
                    UNION ALL
                    SELECT p.collection,c.anchor,p.message_id,p.reply_to_id,p.sent_at,p.body,c.depth+1
                    FROM visible p JOIN lineage c ON p.collection=c.collection AND p.message_id=c.reply_to_id
                    WHERE c.depth<2 AND (p.sent_at,p.message_id)<=(c.sent_at,c.message_id)
                        AND p.message_id<>c.message_id
                ), blocks AS (
                    SELECT collection,anchor AS message_id,array_agg(message_id ORDER BY depth DESC) AS source_ids,
                        to_tsvector('simple',string_agg(body,' ' ORDER BY depth DESC)) AS document
                    FROM lineage GROUP BY collection,anchor
                ) SELECT collection,message_id,source_ids FROM blocks
                WHERE document @@ to_tsquery('simple',%(query)s)
                ORDER BY ts_rank(document,to_tsquery('simple',%(query)s)) DESC,collection,message_id LIMIT 160
                """),
                parameters,
            )
            return await cursor.fetchall(), total
