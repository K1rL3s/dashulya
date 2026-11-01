"""Reject SQL capabilities that could escape a scoped history query."""

import pytest

from dashulya.domain.memory import MemoryInputError
from dashulya.memory.sql_history import validate_query


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM messages",
        "SELECT 1; SELECT 2",
        "WITH gone AS (DELETE FROM messages RETURNING *) SELECT * FROM gone",
        "SELECT * INTO copied FROM messages",
        "SELECT * FROM messages FOR UPDATE",
        "SELECT * FROM public.messages",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM information_schema.tables",
        "SELECT set_config('role','none',true)",
        "SELECT pg_catalog.set_config('search_path','public',true)",
        "SELECT query_to_xml('SELECT * FROM public.messages',true,false,'')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT 'messages'::regclass",
        "SELECT public.lower(text) FROM messages",
        "SELECT 1 OPERATOR(public.+) 2",
        "EXPLAIN SELECT * FROM messages",
        "COPY messages TO STDOUT",
        "WITH settings AS (SELECT set_config('role','none',true)) SELECT * FROM settings",
        "SELECT text COLLATE public.custom FROM messages",
        "SELECT * FROM messages TABLESAMPLE SYSTEM (1)",
    ],
)
def test_unsafe_sql_is_rejected(query: str) -> None:
    with pytest.raises(MemoryInputError):
        validate_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM messages WHERE text ILIKE '%WHERE; DELETE --%' LIMIT 1; -- harmless comment",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 3) SELECT * FROM t",
        "SELECT author_id, count(*) AS n FROM messages GROUP BY author_id HAVING count(*) > 1",
        "SELECT message_id, substring(text from 12001 for 12000) AS excerpt FROM messages",
        "SELECT * FROM messages WHERE topic_id IS NOT DISTINCT FROM NULL",
        "SELECT * FROM messages WHERE sent_at >= TIMESTAMPTZ '2026-10-01 00:00:00+03'",
    ],
)
def test_saved_history_sql_is_accepted_verbatim(query: str) -> None:
    assert validate_query(query) == query
