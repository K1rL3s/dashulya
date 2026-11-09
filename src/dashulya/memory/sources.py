"""Bounded original and reply reads, with a separate page for each selected source."""

import asyncio
import re

from psycopg import sql

from dashulya.database import Database
from dashulya.domain.memory import MemoryAccess, MemoryInputError, SourcePage
from dashulya.memory._scope import VISIBLE_MESSAGES, scope_parameters
from dashulya.memory.store import original


async def read_sources(
    database: Database,
    access: MemoryAccess,
    source_ids: list[str],
    *,
    include_replies: bool,
    offset: int,
    limit: int,
) -> tuple[SourcePage, ...]:
    invalid_sources = any(
        re.fullmatch(r"source:[0-9a-f]{24}:[1-9][0-9]{0,18}", reference) is None
        or int(reference.rsplit(":", 1)[1]) > 2**63 - 1
        for reference in source_ids
    )
    valid_page = (
        type(limit) is int and 1 <= limit <= 20 and type(offset) is int and offset >= 0
    )
    if not 1 <= len(source_ids) <= 10 or not valid_page or invalid_sources:
        message = "Use 1-10 canonical source IDs, limit 1-20 and a nonnegative offset."
        raise MemoryInputError(message)
    async with (
        asyncio.timeout(12),
        database.connection() as connection,
        connection.transaction(),
    ):
        await connection.execute("SET LOCAL statement_timeout = '10s'")
        cursor = await connection.execute(
            sql.SQL("WITH RECURSIVE visible AS NOT MATERIALIZED (")
            + VISIBLE_MESSAGES
            + sql.SQL("""
            ), targets AS (
                SELECT DISTINCT s.reference,m.collection,m.message_id,m.topic_id
                FROM unnest(%(sources)s::text[]) AS s(reference)
                JOIN collections c ON left(encode(sha256(convert_to(c.collection,'UTF8')),'hex'),24)
                    = split_part(s.reference,':',2)
                JOIN visible m ON m.collection=c.collection
                    AND m.message_id=split_part(s.reference,':',3)::bigint
            ), thread(reference,collection,message_id,topic_id) AS (
                SELECT reference,collection,message_id,topic_id FROM targets
                UNION
                SELECT t.reference,m.collection,m.message_id,m.topic_id
                FROM thread t JOIN visible m ON m.collection=t.collection
                    AND m.topic_id IS NOT DISTINCT FROM t.topic_id
                    AND m.reply_to_id=t.message_id AND m.message_id>t.message_id
                WHERE %(replies)s
            ), ranked AS (
                SELECT *,row_number() OVER(PARTITION BY reference ORDER BY message_id) AS position
                FROM thread
            ) SELECT t.reference,r.position,m.* FROM targets t
            LEFT JOIN ranked r ON r.reference=t.reference
                AND r.position>%(offset)s AND r.position<=%(offset)s+%(limit)s+1
            LEFT JOIN visible m ON m.collection=r.collection AND m.message_id=r.message_id
            ORDER BY t.reference,r.position
            """),
            {
                **scope_parameters(access, None),
                "sources": source_ids,
                "replies": include_replies,
                "offset": offset,
                "limit": limit,
            },
        )
        rows = await cursor.fetchall()
    found = {row["reference"] for row in rows}
    return tuple(
        SourcePage(
            reference,
            reference in found,
            tuple(
                original(row)
                for row in rows
                if row["reference"] == reference
                and row["position"] is not None
                and row["position"] <= offset + limit
            ),
            offset + limit
            if any(
                row["reference"] == reference and row["position"] == offset + limit + 1
                for row in rows
            )
            else None,
        )
        for reference in source_ids
    )
