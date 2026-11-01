"""Native BM25 over physically isolated, immutable authorized chat snapshots."""

import bisect
import hashlib
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property, partial
from typing import Any

from psycopg import AsyncConnection, sql

from dashulya.database import Database
from dashulya.memory._unicode61 import tokens
from dashulya.memory._workers import run_in_thread
from dashulya.memory.ranking import reciprocal_rank_fusion

_CACHE_SIZE = 8
_PROFILE = b"pg_search-0.26.4:unicode61-v1:flat:k1=2:b=0.75:"


@dataclass(frozen=True)
class LexicalDocument:
    collection: str
    text: str

    @cached_property
    def key(self) -> str:
        return LexicalIndex.key(self.text)


@dataclass(frozen=True)
class _Snapshot:
    key: str
    collection: str
    rows: tuple[tuple[str, str], ...]
    words: tuple[str, ...]

    @property
    def table(self) -> str:
        return "snapshot_" + self.key[:48]


def _lock_key(key: str) -> int:
    return int.from_bytes(bytes.fromhex(key[:16]), signed=True)


def _expand(prefixes: Sequence[str], words: tuple[str, ...]) -> list[str]:
    found = set()
    for prefix in prefixes:
        position = bisect.bisect_left(words, prefix)
        while position < len(words) and words[position].startswith(prefix):
            found.add(words[position])
            position += 1
    return sorted(found)


class LexicalIndex:
    """Prepare/search authorized blocks; keep at most eight idle native snapshots.

    Callers supply already authorized originals and reply parents. A different
    chat, block version, parent version or corpus produces a separate index.
    PostgreSQL transaction locks protect construction, queries and eviction
    across processes. Failed/cancelled builds roll back their tables and journal.
    The bounded durable cache survives restarts; originals remain authoritative.
    """

    def __init__(self, database: Database) -> None:
        self.database = database
        self._prepared: OrderedDict[str, _Snapshot] = OrderedDict()

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(("unicode61-v1:" + text).encode()).hexdigest()

    async def _snapshots(self, documents: Sequence[LexicalDocument]) -> list[_Snapshot]:
        def group() -> list[tuple[str, str, list[LexicalDocument]]]:
            grouped: dict[str, list[LexicalDocument]] = defaultdict(list)
            for document in documents:
                grouped[document.collection].append(document)
            result = []
            for collection, rows in sorted(grouped.items()):
                keys = "".join(row.key for row in rows).encode()
                key = hashlib.sha256(
                    _PROFILE + collection.encode() + b"\0" + keys
                ).hexdigest()
                result.append((key, collection, rows))
            return result

        snapshots = []
        for key, collection, documents_in_chat in await run_in_thread(group):
            snapshot = self._prepared.get(key)
            if snapshot is None:
                known = {
                    row_key: text
                    for previous in self._prepared.values()
                    if previous.collection == collection
                    for row_key, text in previous.rows
                }

                def normalize(
                    key: str = key,
                    collection: str = collection,
                    rows: list[LexicalDocument] = documents_in_chat,
                    saved: dict[str, str] = known,
                ) -> _Snapshot:
                    prepared = tuple(
                        (
                            row.key,
                            saved[row.key]
                            if row.key in saved
                            else " ".join(tokens(row.text)),
                        )
                        for row in rows
                    )
                    words = tuple(
                        sorted({word for _, text in prepared for word in text.split()})
                    )
                    return _Snapshot(key, collection, prepared, words)

                snapshot = await run_in_thread(normalize)
                self._prepared[key] = snapshot
            self._prepared.move_to_end(key)
            while len(self._prepared) > _CACHE_SIZE:
                self._prepared.popitem(last=False)
            snapshots.append(snapshot)
        return snapshots

    @staticmethod
    async def _ensure(
        connection: AsyncConnection[dict[str, Any]], snapshot: _Snapshot
    ) -> str:
        # Lock before inspecting the journal: another process may be committing
        # the same index, or collecting this idle snapshot, on another connection.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(%s)", (_lock_key(snapshot.key),)
        )
        cursor = await connection.execute(
            "SELECT c.namespace,pg_get_userbyid(n.nspowner) AS owner "
            "FROM lexical_cache_config c JOIN pg_namespace n ON n.nspname=c.namespace"
        )
        config = await cursor.fetchone()
        if config is None:
            msg = "ParadeDB cache migrations are required."
            raise RuntimeError(msg)
        namespace = str(config["namespace"])
        cursor = await connection.execute(
            "SELECT snapshot_key FROM lexical_snapshots WHERE snapshot_key=%s",
            (snapshot.key,),
        )
        if await cursor.fetchone() is None:
            table = sql.Identifier(namespace, snapshot.table)
            await connection.execute(
                sql.SQL(
                    "CREATE TABLE {} (id bigint PRIMARY KEY,document_key text UNIQUE NOT NULL,"
                    "collection text NOT NULL CHECK(collection={}),flat text NOT NULL)"
                ).format(table, sql.Literal(snapshot.collection))
            )
            # Administrative maintenance can warm the cache too. Keep its
            # relations owned by the runtime role, including when built by admin.
            await connection.execute(
                sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    table, sql.Identifier(config["owner"])
                )
            )
            async with connection.cursor().copy(
                sql.SQL("COPY {} FROM STDIN").format(table)
            ) as writer:
                for number, (key, flat) in enumerate(snapshot.rows, 1):
                    await writer.write_row((number, key, snapshot.collection, flat))
            await connection.execute(
                sql.SQL(
                    "CREATE INDEX ON {} USING paradedb (id,(flat::pdb.whitespace("
                    "'lowercase=false','pnorms=true','k1=2.0','b=0.75')))"
                ).format(table)
            )
            await connection.execute(sql.SQL("ANALYZE {}").format(table))
            await connection.execute(
                "INSERT INTO lexical_snapshots(snapshot_key,table_name,collection) VALUES(%s,%s,%s)",
                (snapshot.key, snapshot.table, snapshot.collection),
            )
        await connection.execute(
            "UPDATE lexical_snapshots SET used_at=clock_timestamp() WHERE snapshot_key=%s",
            (snapshot.key,),
        )
        return namespace

    @staticmethod
    async def _collect(
        connection: AsyncConnection[dict[str, Any]], namespace: str
    ) -> None:
        guard = hashlib.sha256((namespace + ":gc").encode()).hexdigest()
        cursor = await connection.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS acquired", (_lock_key(guard),)
        )
        row = await cursor.fetchone()
        if not row or not row["acquired"]:
            return
        cursor = await connection.execute(
            "SELECT snapshot_key,table_name FROM lexical_snapshots ORDER BY used_at DESC,snapshot_key OFFSET %s",
            (_CACHE_SIZE,),
        )
        for entry in await cursor.fetchall():
            cursor = await connection.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS acquired",
                (_lock_key(entry["snapshot_key"]),),
            )
            acquired = await cursor.fetchone()
            if acquired and acquired["acquired"]:
                await connection.execute(
                    sql.SQL("DROP TABLE {}").format(
                        sql.Identifier(namespace, entry["table_name"])
                    )
                )
                await connection.execute(
                    "DELETE FROM lexical_snapshots WHERE snapshot_key=%s",
                    (entry["snapshot_key"],),
                )

    async def prepare(self, documents: Sequence[LexicalDocument]) -> None:
        for snapshot in await self._snapshots(documents):
            async with self.database.connection() as connection, connection.transaction():
                namespace = await self._ensure(connection, snapshot)
                await self._collect(connection, namespace)

    async def search(
        self,
        documents: Sequence[LexicalDocument],
        eligible: Sequence[str],
        prefixes: Sequence[str],
        *,
        limit: int,
    ) -> list[str]:
        normalized = [term for prefix in prefixes for term in tokens(prefix)]
        if not documents or not eligible or not normalized or limit <= 0:
            return []
        allowed = set(eligible)
        rankings = []
        for snapshot in await self._snapshots(documents):
            expanded = await run_in_thread(partial(_expand, normalized, snapshot.words))
            ids = [
                number
                for number, (key, _) in enumerate(snapshot.rows, 1)
                if key in allowed
            ]
            if not expanded or not ids:
                continue
            async with self.database.connection() as connection, connection.transaction():
                namespace = await self._ensure(connection, snapshot)
                query = sql.SQL(
                    "SELECT id FROM {} WHERE flat @@@ pdb.match_disjunction(%s::text[])"
                ).format(sql.Identifier(namespace, snapshot.table))
                parameters: list[Any] = [expanded]
                if len(ids) != len(snapshot.rows):
                    query += sql.SQL(" AND id=ANY(%s::bigint[])")
                    parameters.append(ids)
                query += sql.SQL(" ORDER BY pdb.score(id) DESC,id LIMIT %s")
                parameters.append(limit)
                cursor = await connection.execute(
                    query,
                    parameters,
                )
                rankings.append([
                    snapshot.rows[row["id"] - 1][0] for row in await cursor.fetchall()
                ])
                await self._collect(connection, namespace)
        if len(rankings) == 1:
            return rankings[0]
        order = {document.key: position for position, document in enumerate(documents)}
        return [key for key, _ in reciprocal_rank_fusion(rankings, tie_order=order)][
            :limit
        ]
