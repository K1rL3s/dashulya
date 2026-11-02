"""Bounded candidate selection in PostgreSQL; no original text is read here."""

import asyncio
import json
from dataclasses import dataclass
from operator import itemgetter
from typing import Any

from psycopg import sql

from dashulya.domain.memory import MemoryAccess, MemoryInputError
from dashulya.memory._timing import search_stage
from dashulya.memory.hybrid import _date, query_terms
from dashulya.memory.pending_search import PendingSearch
from dashulya.memory.ranking import reciprocal_rank_fusion, select_source_diverse
from dashulya.memory.search_index import SearchIndex

_CANDIDATES = 160


@dataclass(frozen=True)
class Candidate:
    collection: str
    message_id: int
    source_ids: tuple[int, ...]
    score: float


def _condition() -> sql.SQL:
    return sql.SQL("""(%(private)s OR d.chat_id=%(origin)s) AND (%(author)s::bigint IS NULL OR d.author_id=%(author)s)
        AND (%(after)s::timestamptz IS NULL OR d.sent_at>=%(after)s)
        AND (%(before)s::timestamptz IS NULL OR d.sent_at<=%(before)s)
        AND (%(cutoff)s::timestamptz IS NULL OR (
            (d.edited_at IS NULL OR d.edited_at<%(cutoff)s)
            AND CASE WHEN %(cutoff_id)s::bigint IS NULL OR NOT %(same_chat)s
                THEN d.sent_at<%(cutoff)s
                ELSE (d.sent_at<%(cutoff)s OR (d.sent_at=%(cutoff)s AND d.message_id<%(cutoff_id)s)) END))
        AND CASE WHEN %(cutoff)s::timestamptz IS NULL THEN d.valid_until IS NULL
            ELSE (d.valid_after IS NULL OR d.valid_after<%(cutoff)s)
                AND (d.valid_until IS NULL OR d.valid_until>=%(cutoff)s) END
        AND d.message_id <> ALL(ARRAY(SELECT p.message_id FROM search_pending p
            WHERE p.collection=%(collection)s))""")


class IndexedSearch:
    def __init__(self, index: SearchIndex, access: MemoryAccess) -> None:
        self.index = index
        self.access = access

    async def _count(self, chat: str, parameters: dict[str, Any]) -> dict[str, int]:
        async with self.index.database.connection() as connection:
            row = await (
                await connection.execute(
                    sql.SQL(
                        "SELECT count(*) AS source_count,count(*) FILTER(WHERE embedded) AS embedded_blocks FROM {} d WHERE "
                    ).format(self.index.metadata(chat))
                    + _condition(),
                    parameters,
                )
            ).fetchone()
            assert row is not None
            return {key: int(value) for key, value in row.items()}

    async def _rank(
        self,
        chat: str,
        parameters: dict[str, Any],
        *,
        query: list[str],
        vector: list[float] | None,
    ) -> list[dict[str, Any]]:
        stage = "dense_search" if vector is not None else "lexical_search"
        with search_stage(stage):
            async with (
                self.index.database.connection() as connection,
                connection.transaction(),
            ):
                # Generic plans can lose ParadeDB's score-producing custom scan.
                # Custom plans also use the actual scope/filter selectivity.
                await connection.execute("SET LOCAL plan_cache_mode='force_custom_plan'")
                if vector is not None:
                    await connection.execute(
                        "SET LOCAL hnsw.iterative_scan='relaxed_order'"
                    )
                    await connection.execute("SET LOCAL hnsw.ef_search=400")
                    # The direct distance ORDER BY is required for an HNSW scan.
                    statement = (
                        sql.SQL(
                            "WITH candidates AS MATERIALIZED (SELECT d.message_id,d.source_ids,d.embedding <#> %(vector)s::vector AS distance "
                            "FROM {} d WHERE d.embedding IS NOT NULL AND "
                        ).format(self.index.table(chat))
                        + _condition()
                        + sql.SQL(
                            " ORDER BY d.embedding <#> %(vector)s::vector LIMIT 2000) "
                            "SELECT * FROM candidates ORDER BY distance+0,message_id LIMIT 160"
                        )
                    )
                    parameters = {**parameters, "vector": json.dumps(vector)}
                else:
                    await connection.execute("SET LOCAL enable_seqscan=off")
                    predicate = sql.SQL(" OR ").join(
                        sql.SQL(
                            "d.flat @@@ pdb.phrase_prefix(ARRAY[{}::text],2147483647)"
                        ).format(sql.Placeholder(f"prefix{number}"))
                        for number in range(len(query))
                    )
                    statement = (
                        sql.SQL(
                            "SELECT d.message_id,d.source_ids FROM {} d WHERE ("
                        ).format(self.index.table(chat))
                        + predicate
                        + sql.SQL(") AND ")
                        + _condition()
                        + sql.SQL(" ORDER BY pdb.score(d.id) DESC,d.message_id LIMIT 160")
                    )
                    parameters = {
                        **parameters,
                        **{
                            f"prefix{number}": prefix
                            for number, prefix in enumerate(query)
                        },
                    }
                rows = await (await connection.execute(statement, parameters)).fetchall()
                return [{**row, "collection": chat} for row in rows]

    async def search(
        self,
        query: str,
        *,
        chat: str | None,
        author_id: int | None,
        after: str | None,
        before: str | None,
        limit: int,
    ) -> tuple[list[Candidate], dict[str, int]]:
        parameters = await self._parameters(chat, author_id, after, before)
        with search_stage("index_coverage"):
            counts = await asyncio.gather(
                *(self._count(name, parameters[name]) for name in parameters)
            )
        coverage = {
            key: sum(row[key] for row in counts)
            for key in ("source_count", "embedded_blocks")
        }
        coverage["pending_blocks"] = (
            coverage["source_count"] - coverage["embedded_blocks"]
        )
        prefixes = query_terms(query)
        with search_stage("pending_search"):
            pending_rows, pending_count = await PendingSearch(
                self.index.database, self.access
            ).search(
                prefixes,
                chat=chat,
                author_id=author_id,
                after=_date(after),
                before=_date(before),
            )
        coverage["source_count"] += pending_count
        coverage["pending_blocks"] += pending_count
        coverage["unindexed_blocks"] = pending_count
        if not coverage["source_count"] or limit <= 0:
            return [], coverage

        rankings = await self._rankings(
            parameters, prefixes, query, coverage["embedded_blocks"]
        )
        return _fuse(rankings, pending_rows, limit), coverage

    async def _parameters(
        self,
        chat: str | None,
        author_id: int | None,
        after: str | None,
        before: str | None,
    ) -> dict[str, dict[str, Any]]:
        lower, upper = _date(after), _date(before)
        if lower is not None and upper is not None and lower > upper:
            message = "after must not be later than before."
            raise MemoryInputError(message)
        chats = await self.index.bindings(
            chat_id=None if self.access.is_private else self.access.chat_id,
            collection=chat,
        )
        return {
            name: {
                "collection": name,
                "author": author_id,
                "after": lower,
                "before": upper,
                "cutoff": self.access.before,
                "cutoff_id": self.access.before_id,
                "same_chat": chats[name] == self.access.chat_id,
                "private": self.access.is_private,
                "origin": self.access.chat_id,
            }
            for name in chats
        }

    async def _rankings(
        self,
        parameters: dict[str, dict[str, Any]],
        prefixes: list[str],
        query: str,
        embedded: int,
    ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
        async def lexical() -> list[list[dict[str, Any]]]:
            if not prefixes:
                return []
            return await asyncio.gather(
                *(
                    self._rank(name, parameters[name], query=prefixes, vector=None)
                    for name in parameters
                )
            )

        async def dense() -> list[dict[str, Any]]:
            if not embedded:
                return []
            with search_stage("query_embedding"):
                vector = (await self.index.vectors.vectors([query]))[query]
            rows = await asyncio.gather(
                *(
                    self._rank(name, parameters[name], query=[], vector=vector)
                    for name in parameters
                )
            )
            return sorted(
                (row for group in rows for row in group),
                key=itemgetter("distance", "collection", "message_id"),
            )[:_CANDIDATES]

        lexical_task = asyncio.create_task(lexical())
        dense_task = asyncio.create_task(dense())
        tasks = [lexical_task, dense_task]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return lexical_task.result(), dense_task.result()


def _fuse(
    rankings: tuple[list[list[dict[str, Any]]], list[dict[str, Any]]],
    pending_rows: list[dict[str, Any]],
    limit: int,
) -> list[Candidate]:
    lexical_rows, dense_rows = rankings
    for collection in sorted({row["collection"] for row in pending_rows}):
        lexical_rows.append([
            row for row in pending_rows if row["collection"] == collection
        ])
    metadata = {
        (row["collection"], row["message_id"]): row
        for group in [*lexical_rows, dense_rows]
        for row in group
    }
    order = {key: position for position, key in enumerate(sorted(metadata))}
    lexical_ranks = [
        [(row["collection"], row["message_id"]) for row in group]
        for group in lexical_rows
    ]
    # Independent chats have independent BM25 statistics; merge ordinal ranks.
    lexical_keys = [
        key for key, _ in reciprocal_rank_fusion(lexical_ranks, tie_order=order)
    ][:_CANDIDATES]
    dense_keys = [(row["collection"], row["message_id"]) for row in dense_rows]
    fused = reciprocal_rank_fusion([lexical_keys, dense_keys], tie_order=order)
    candidates = [
        Candidate(key[0], key[1], tuple(metadata[key]["source_ids"]), score)
        for key, score in fused
    ]
    return select_source_diverse(
        candidates,
        anchor=lambda c: (c.collection, c.message_id),
        sources=lambda c: ((c.collection, number) for number in c.source_ids),
        limit=min(limit, 8),
    )
