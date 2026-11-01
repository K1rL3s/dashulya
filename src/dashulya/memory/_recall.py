"""Bounded recall and source reads over an authorized canonical snapshot."""

from datetime import UTC, datetime, timedelta
from typing import Any

from dashulya.domain.memory import MemoryInputError
from dashulya.memory._engines import EngineCache
from dashulya.memory._selection import RecallSelection
from dashulya.memory._timing import search_stage, search_trace
from dashulya.memory.history import CanonicalHistory


def _history_before(value: str | None, after: str | None) -> str | None:
    """Translate recall's inclusive upper bound to canonical history's exclusive one."""
    if value is None:
        return None
    try:
        upper = datetime.fromisoformat(value)
        lower = datetime.fromisoformat(after) if after is not None else None
    except ValueError:
        error_message = "Search dates must use ISO 8601 with a timezone."
        raise MemoryInputError(error_message) from None
    if upper.utcoffset() is None:
        error_message = "Search dates must include a timezone."
        raise MemoryInputError(error_message)
    if lower is not None:
        if lower.utcoffset() is None:
            error_message = "Search dates must include a timezone."
            raise MemoryInputError(error_message)
        if lower > upper:
            error_message = "after must not be later than before."
            raise MemoryInputError(error_message)
    try:
        upper = upper.astimezone(UTC)
    except OverflowError:
        error_message = "Search date is outside the supported UTC range."
        raise MemoryInputError(error_message) from None
    if upper == datetime.max.replace(tzinfo=UTC):
        # Every representable canonical timestamp satisfies this upper bound.
        return None
    return (upper + timedelta(microseconds=1)).isoformat()


class ScopedRecall:
    def __init__(self, history: CanonicalHistory, engines: EngineCache) -> None:
        self.history = history
        self._engines = engines

    async def search(
        self,
        query: str = "",
        *,
        memory_id: str | None = None,
        chat: str | None = None,
        author_id: int | None = None,
        after: str | None = None,
        before: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        with search_trace(self.history.access) as metrics:
            if memory_id is not None:
                return await self.history.query(
                    "list",
                    source_id=memory_id,
                    limit=1,
                    chat=chat,
                    author_id=author_id,
                    after=after,
                    before=_history_before(before, after),
                )
            selection = RecallSelection(self.history)
            with search_stage("coverage"):
                available = await selection.coverage(chat)
            if not query.strip():
                return {"hits": [], "coverage": available}
            with search_stage("selection"):
                originals, primary_ids = await selection.originals(
                    chat=chat, author_id=author_id, after=after, before=before
                )
            metrics["candidate_count"] = len(primary_ids)
            metrics["loaded_originals"] = len(originals)
            with search_stage("retrieval"):
                async with self._engines.lease(
                    originals, primary_ids=primary_ids
                ) as engine:
                    hits = await engine.search(query, limit=limit)
                    with search_stage("index_coverage"):
                        coverage = await engine.coverage()
                    with search_stage("context"):
                        context_originals = await selection.context(
                            [hit.sources[-1] for hit in hits], chat=chat
                        )
                    output = []
                    remaining = 48_000
                    for hit in hits:
                        sources = []
                        for source in hit.sources:
                            item = source.render(max_chars=min(12_000, max(0, remaining)))
                            remaining -= len(item["text"])
                            sources.append(item)
                        context = self.history.related(hit.sources[-1], context_originals)
                        output.append({
                            "memory_id": hit.sources[-1].source_id,
                            "kind": "message",
                            "memory": hit.text[:6000],
                            "score": hit.score,
                            "sources": sources,
                            "context": [item.render(max_chars=3000) for item in context],
                        })
                    return {
                        "hits": output,
                        "coverage": {
                            **coverage,
                            **available,
                            "complete_history": False,
                            "notice": "Only observed and imported messages are available. Search is bounded; use chat_history for exact counts or original-message scans.",
                        },
                    }
