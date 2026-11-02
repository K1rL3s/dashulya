"""Application-owned memory: scoped readers and coordinated resource lifetimes."""

from dashulya.domain.memory import MemoryAccess, MemoryBackend
from dashulya.memory._indexing import IndexingWorker
from dashulya.memory._recall import ScopedRecall
from dashulya.memory.history import CanonicalHistory
from dashulya.memory.search_index import SearchIndex
from dashulya.memory.store import MessageStore
from dashulya.memory.vectors import EmbeddingCache


class HybridChatMemory:
    def __init__(
        self,
        store: MessageStore,
        vectors: EmbeddingCache,
        *,
        owner_user_id: int,
    ) -> None:
        self._store = store
        self._index = SearchIndex(store.database, vectors)
        self._indexing = IndexingWorker(
            store,
            vectors,
            self._index,
            owner_user_id=owner_user_id,
        )

    def history(self, access: MemoryAccess) -> CanonicalHistory:
        return CanonicalHistory(self._store, access)

    def recall(self, access: MemoryAccess) -> MemoryBackend:
        return ScopedRecall(self.history(access), self._index)

    async def refresh_once(self) -> None:
        await self._indexing.refresh_once()

    async def prepare_cached(self) -> None:
        """Prepare derived indexes using existing vectors without provider calls."""
        while await self._index.prepare():
            pass
        await self._index.finish()

    async def start(self) -> None:
        await self._indexing.start()

    async def close(self) -> None:
        # Stop preparation before draining readers, then let DI close shared I/O.
        await self._indexing.close()
        await self._index.close()
