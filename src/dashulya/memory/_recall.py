"""Bounded recall and source reads over an authorized canonical snapshot."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from dashulya.domain.memory import MemoryInputError, OriginalMessage
from dashulya.memory._selection import RecallSelection
from dashulya.memory._timing import search_stage, search_trace
from dashulya.memory.history import CanonicalHistory
from dashulya.memory.hybrid import _date, _render
from dashulya.memory.indexed_search import Candidate, IndexedSearch
from dashulya.memory.search_index import SearchIndex


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
    def __init__(self, history: CanonicalHistory, index: SearchIndex) -> None:
        self.history = history
        self.index = index

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
            self.history._chat(chat)
            if not query.strip():
                return {"hits": [], "coverage": await self._available(selection, chat)}
            async with self.index.lease():
                hits, coverage, available = await self._lookup(
                    selection,
                    query,
                    chat=chat,
                    author_id=author_id,
                    after=after,
                    before=before,
                    limit=limit,
                )
                metrics["candidate_count"] = coverage["source_count"]
                output, metrics["loaded_originals"] = await self._evidence(
                    selection,
                    hits,
                    chat=chat,
                    author_id=author_id,
                    after=after,
                    before=before,
                )
                return {
                    "hits": output,
                    "coverage": {
                        **coverage,
                        **available,
                        "complete_history": False,
                        "notice": "Only observed and imported messages are available. Search is bounded; use chat_history for exact counts or original-message scans.",
                    },
                }

    @staticmethod
    async def _available(selection: RecallSelection, chat: str | None) -> dict[str, Any]:
        with search_stage("coverage"):
            return await selection.coverage(chat)

    async def _lookup(
        self,
        selection: RecallSelection,
        query: str,
        *,
        chat: str | None,
        author_id: int | None,
        after: str | None,
        before: str | None,
        limit: int,
    ) -> tuple[list[Candidate], dict[str, int], dict[str, Any]]:
        await self.index.initialize()
        available = asyncio.create_task(self._available(selection, chat))
        ranking = asyncio.create_task(
            IndexedSearch(self.index, self.history.access).search(
                query,
                chat=chat,
                author_id=author_id,
                after=after,
                before=before,
                limit=limit,
            )
        )
        tasks = [available, ranking]
        try:
            with search_stage("retrieval"):
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return *ranking.result(), available.result()

    async def _evidence(
        self,
        selection: RecallSelection,
        hits: list[Candidate],
        *,
        chat: str | None,
        author_id: int | None,
        after: str | None,
        before: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        keys = sorted({
            (hit.collection, number) for hit in hits for number in hit.source_ids
        })
        with search_stage("selection"):
            originals = await selection.selected(keys, chat=chat)
        by_key = {(m.collection, m.message_id): m for m in originals}
        anchors = [
            by_key[h.collection, h.message_id]
            for h in hits
            if (h.collection, h.message_id) in by_key
        ]
        with search_stage("context"):
            context = await selection.context(anchors, chat=chat)
        evidence = _Evidence(
            self.history, by_key, context, author_id, _date(after), _date(before)
        )
        return evidence.render(hits), len(originals)


@dataclass
class _Evidence:
    history: CanonicalHistory
    originals: dict[tuple[str, int], OriginalMessage]
    context: list[OriginalMessage]
    author_id: int | None
    lower: datetime | None
    upper: datetime | None

    def _matches(self, source: OriginalMessage) -> bool:
        return (
            (self.author_id is None or source.author_id == self.author_id)
            and (
                self.lower is None
                or (source.sent_at is not None and source.sent_at >= self.lower)
            )
            and (
                self.upper is None
                or (source.sent_at is not None and source.sent_at <= self.upper)
            )
        )

    def render(self, hits: list[Candidate]) -> list[dict[str, Any]]:
        output = []
        remaining = 48_000
        for hit in hits:
            anchor = self.originals.get((hit.collection, hit.message_id))
            if anchor is None:
                continue
            sources = []
            rendered = []
            for number in hit.source_ids:
                source = self.originals.get((hit.collection, number))
                if source is None or not self._matches(source):
                    continue
                item = source.render(max_chars=min(12_000, max(0, remaining)))
                remaining -= len(item["text"])
                sources.append(item)
                rendered.append(_render(source))
            context = self.history.related(anchor, self.context)
            output.append({
                "memory_id": anchor.source_id,
                "kind": "message",
                "memory": "\n\n".join(rendered)[:6000],
                "score": hit.score,
                "sources": sources,
                "context": [item.render(max_chars=3000) for item in context],
            })
        return output
