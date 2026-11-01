"""Bounded immutable metadata engines with concurrent leases and shutdown draining."""

import asyncio
from collections import OrderedDict
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from dashulya.domain.memory import OriginalMessage
from dashulya.memory._workers import run_in_thread
from dashulya.memory.hybrid import HybridMemory
from dashulya.memory.lexical import LexicalIndex
from dashulya.memory.vectors import EmbeddingCache


@dataclass
class _CachedEngine:
    engine: HybridMemory
    users: int = 0


class EngineCache:
    """Lease engines for exact snapshots; retain at most two idle cache entries.

    Leased entries survive eviction/invalidation. Closing rejects new leases
    and waits for every borrower, including cancelled searches, to release one.
    """

    def __init__(self, vectors: EmbeddingCache, lexical: LexicalIndex) -> None:
        self.vectors = vectors
        self.lexical = lexical
        self._engines: OrderedDict[
            tuple[int, tuple[OriginalMessage, ...], frozenset[str] | None], _CachedEngine
        ] = OrderedDict()
        self._engine_lock = asyncio.Lock()
        self._index_generation = 0
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()

    def _evict(self) -> None:
        for key in list(self._engines):
            if len(self._engines) <= 2:
                break
            entry = self._engines[key]
            if entry.users == 0:
                del self._engines[key]

    @asynccontextmanager
    async def lease(
        self,
        originals: Sequence[OriginalMessage],
        *,
        primary_ids: frozenset[str] | None = None,
    ) -> AsyncGenerator[HybridMemory, None]:
        # Frozen originals are value keys: equality checks every field, including
        # private file references, without serializing all message text to JSON.
        snapshot = tuple(originals)
        async with self._engine_lock:
            key = (self._index_generation, snapshot, primary_ids)
            if self._closed:
                error_message = "Memory retrieval is closed."
                raise RuntimeError(error_message)
            entry = self._engines.get(key)
            if entry is None:
                engine = await run_in_thread(
                    lambda: HybridMemory(
                        originals, self.vectors, self.lexical, primary_ids=primary_ids
                    )
                )
                entry = _CachedEngine(engine)
                self._engines[key] = entry
            self._engines.move_to_end(key)
            entry.users += 1
            self._idle.clear()
        try:
            async with self._engine_lock:
                self._evict()
            yield entry.engine
        finally:
            async with self._engine_lock:
                entry.users -= 1
                if not any(item.users for item in self._engines.values()):
                    self._idle.set()
                self._evict()

    async def invalidate(self) -> None:
        """Use fresh engines for subsequent leases after index preparation."""
        async with self._engine_lock:
            self._index_generation += 1
            # Completed vectors can make a formerly pending block eligible for dense recall.
            for key, entry in list(self._engines.items()):
                if entry.users == 0:
                    del self._engines[key]

    async def close(self) -> None:
        async with self._engine_lock:
            self._closed = True
        await self._idle.wait()
        async with self._engine_lock:
            self._engines.clear()
