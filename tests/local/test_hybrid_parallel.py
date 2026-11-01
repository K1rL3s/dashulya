import asyncio
from typing import cast
from unittest.mock import Mock

import pytest

from dashulya.memory.hybrid import HybridMemory
from dashulya.memory.lexical import LexicalIndex
from dashulya.memory.vectors import EmbeddingCache
from tests.originals import original


@pytest.fixture
def hybrid() -> tuple[HybridMemory, Mock, Mock]:
    lexical = Mock(spec=LexicalIndex)
    lexical.key.side_effect = LexicalIndex.key
    lexical.search.side_effect = lambda _corpus, eligible, _terms, **_kwargs: eligible
    vectors = Mock(spec=EmbeddingCache)
    vectors.key.side_effect = lambda text: "vector:" + LexicalIndex.key(text)
    vectors.saved_keys.side_effect = set
    vectors.vectors.return_value = {"needle": [1.0] + [0.0] * 511}
    vectors.nearest.side_effect = lambda keys, _query, **_kwargs: keys
    memory = HybridMemory(
        [original(1, "needle")],
        cast("EmbeddingCache", vectors),
        cast("LexicalIndex", lexical),
    )
    return memory, lexical, vectors


async def test_embedding_overlaps_preparation_and_dense_overlaps_bm25(
    hybrid: tuple[HybridMemory, Mock, Mock],
) -> None:
    memory, lexical, vectors = hybrid
    preparing, embedding, bm25, dense = (asyncio.Event() for _ in range(4))

    async def prepare(_texts: list[str]) -> None:
        preparing.set()
        await embedding.wait()

    async def embed(_texts: list[str]) -> dict[str, list[float]]:
        embedding.set()
        await preparing.wait()
        return {"needle": [1.0] + [0.0] * 511}

    async def lexical_search(
        _corpus: list[str], eligible: list[str], _terms: list[str], **_kwargs: object
    ) -> list[str]:
        bm25.set()
        await dense.wait()
        return eligible

    async def nearest(
        keys: list[str], _query: list[float], **_kwargs: object
    ) -> list[str]:
        dense.set()
        await bm25.wait()
        return keys

    lexical.prepare.side_effect = prepare
    lexical.search.side_effect = lexical_search
    vectors.vectors.side_effect = embed
    vectors.nearest.side_effect = nearest
    hits = await asyncio.wait_for(memory.search("needle"), 2)
    assert [hit.sources[-1].message_id for hit in hits] == [1]
    assert hits[0].score == pytest.approx(2 / 61)


@pytest.mark.parametrize("failing", ["lexical", "dense"])
async def test_branch_failure_cancels_and_joins_its_sibling(
    hybrid: tuple[HybridMemory, Mock, Mock], failing: str
) -> None:
    memory, lexical, vectors = hybrid
    started = {name: asyncio.Event() for name in ("lexical", "dense")}
    cleaned = asyncio.Event()

    async def branch(name: str) -> None:
        started[name].set()
        await started["dense" if name == "lexical" else "lexical"].wait()
        if name == failing:
            raise RuntimeError(name + " unavailable")
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def lexical_search(*_args: object, **_kwargs: object) -> list[str]:
        await branch("lexical")
        return []

    async def embed(_texts: list[str]) -> dict[str, list[float]]:
        await branch("dense")
        return {}

    lexical.search.side_effect = lexical_search
    vectors.vectors.side_effect = embed
    with pytest.raises(RuntimeError, match=failing + " unavailable"):
        await asyncio.wait_for(memory.search("needle"), 2)
    assert cleaned.is_set()


async def test_request_cancellation_waits_for_both_branches(
    hybrid: tuple[HybridMemory, Mock, Mock],
) -> None:
    memory, lexical, vectors = hybrid
    started = [asyncio.Event(), asyncio.Event()]
    cleaned: set[int] = set()

    async def block(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.add(index)

    async def prepare(_texts: list[str]) -> None:
        await block(0)

    async def embed(_texts: list[str]) -> dict[str, list[float]]:
        await block(1)
        return {}

    lexical.prepare.side_effect = prepare
    vectors.vectors.side_effect = embed
    task = asyncio.create_task(memory.search("needle"))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cleaned == {0, 1}


async def test_missing_document_vectors_use_only_lexical_ranking(
    hybrid: tuple[HybridMemory, Mock, Mock],
) -> None:
    memory, _lexical, vectors = hybrid
    vectors.saved_keys.side_effect = None
    vectors.saved_keys.return_value = set()
    hits = await memory.search("needle")
    assert hits[0].score == pytest.approx(1 / 61)
    vectors.vectors.assert_not_awaited()
    vectors.nearest.assert_not_awaited()


async def test_empty_filtered_snapshot_starts_neither_branch(
    hybrid: tuple[HybridMemory, Mock, Mock],
) -> None:
    memory, lexical, vectors = hybrid
    assert await memory.search("needle", author_id=999) == []
    lexical.prepare.assert_not_awaited()
    lexical.search.assert_not_awaited()
    vectors.saved_keys.assert_not_awaited()
    vectors.vectors.assert_not_awaited()
