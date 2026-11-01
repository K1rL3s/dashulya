"""PostgreSQL postings and BM25 over the caller-authorized document snapshot."""

import hashlib
from collections import Counter
from collections.abc import Sequence

from psycopg import sql

from dashulya.database import Database
from dashulya.memory._unicode61 import tokens


def _prefix_end(prefix: str) -> str | None:
    """Exclusive UTF-8 ordering bound; a maximal suffix carries left."""
    for index in range(len(prefix) - 1, -1, -1):
        code = ord(prefix[index])
        if code < 0x10FFFF:
            return prefix[:index] + chr(0xE000 if code == 0xD7FF else code + 1)
    return None


def _prefix_matches(prefixes: Sequence[str]) -> sql.Composable:
    # Expose constant bounds to the planner, including on prepared connections.
    # A dynamic LIKE join cannot derive a B-tree range. Constant branches also
    # give it prefix-specific selectivity rather than a generic inequality cost.
    branches: list[sql.Composable] = []
    for phrase, prefix in enumerate(prefixes, 1):
        upper = _prefix_end(prefix)
        branches.append(
            sql.SQL(
                "SELECT document_key,{}::bigint AS phrase,frequency FROM lexical_terms "
                "WHERE octet_length(term)<2000 AND term ~>=~ {} {} AND term LIKE {}"
            ).format(
                sql.Literal(phrase),
                sql.Literal(prefix),
                sql.SQL("AND term ~<~ {}").format(sql.Literal(upper))
                if upper is not None
                else sql.SQL(""),
                sql.Literal(prefix + "%%"),
            )
        )
    branches.append(
        sql.SQL("""
        SELECT t.document_key,q.phrase,t.frequency FROM lexical_terms t
        JOIN unnest(%(prefixes)s::text[]) WITH ORDINALITY q(prefix,phrase)
            ON t.term LIKE q.prefix || '%%'
        WHERE octet_length(t.term)>=2000
    """)
    )
    return sql.SQL(" UNION ALL ").join(branches)


class LexicalIndex:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(("unicode61-v1:" + text).encode()).hexdigest()

    async def prepare(self, texts: Sequence[str]) -> None:
        keyed = {self.key(text): text for text in texts}
        if not keyed:
            return
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT document_key FROM lexical_documents WHERE document_key=ANY(%s)",
                (list(keyed),),
            )
            saved = {row["document_key"] for row in await cursor.fetchall()}
            # Concurrent cold searches can prepare overlapping document sets.
            # A stable insertion order avoids waiting on each other's later keys.
            for key in sorted(keyed.keys() - saved):
                frequencies = Counter(tokens(keyed[key]))
                async with connection.transaction():
                    inserted = await connection.execute(
                        "INSERT INTO lexical_documents VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING document_key",
                        (key, sum(frequencies.values())),
                    )
                    if await inserted.fetchone() is None:
                        continue
                    async with connection.cursor() as writer:
                        await writer.executemany(
                            "INSERT INTO lexical_terms(document_key,term,frequency,term_key) VALUES (%s,%s,%s,%s)",
                            [
                                (
                                    key,
                                    term,
                                    count,
                                    hashlib.sha256(term.encode()).hexdigest(),
                                )
                                for term, count in frequencies.items()
                            ],
                        )

    async def search(
        self,
        corpus: Sequence[str],
        eligible: Sequence[str],
        prefixes: Sequence[str],
        *,
        limit: int,
    ) -> list[str]:
        normalized = [term for prefix in prefixes for term in tokens(prefix)]
        if not corpus or not eligible or not normalized:
            return []
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                sql.SQL("""WITH corpus AS MATERIALIZED (
                    SELECT d.*,a.position FROM lexical_documents d
                    JOIN unnest(%(corpus)s::text[]) WITH ORDINALITY a(document_key,position)
                        ON d.document_key=a.document_key
                ), stats AS (
                    SELECT count(*)::float8 AS n,avg(word_count)::float8 AS avgdl FROM corpus
                ), matches AS MATERIALIZED (
                    SELECT t.document_key,t.phrase,sum(t.frequency)::float8 AS tf
                    FROM ({prefix_matches}) t JOIN corpus c USING(document_key)
                    GROUP BY t.document_key,t.phrase
                ), frequencies AS (
                    SELECT phrase,count(*)::float8 AS df FROM matches GROUP BY phrase
                ), scores AS (
                    SELECT m.document_key,c.position,
                        sum((CASE WHEN ln((s.n-f.df+0.5)/(f.df+0.5))<=0 THEN 1e-6
                            ELSE ln((s.n-f.df+0.5)/(f.df+0.5)) END)
                            *m.tf*2.2/(m.tf+1.2*(0.25+0.75*c.word_count/NULLIF(s.avgdl,0)))) AS score
                    FROM matches m JOIN corpus c USING(document_key)
                    JOIN frequencies f USING(phrase) CROSS JOIN stats s
                    GROUP BY m.document_key,c.position
                ) SELECT document_key FROM scores WHERE document_key=ANY(%(eligible)s)
                    ORDER BY score DESC,position LIMIT %(limit)s""").format(
                    prefix_matches=_prefix_matches(normalized)
                ),
                {
                    "corpus": list(corpus),
                    "eligible": list(eligible),
                    "prefixes": normalized,
                    "limit": limit,
                },
            )
            return [row["document_key"] for row in await cursor.fetchall()]
