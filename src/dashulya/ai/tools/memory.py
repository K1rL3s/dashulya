import json
from datetime import datetime
from typing import Annotated

from ag2 import Variable
from ag2.tools import tool
from dishka_ag2 import FromDishka, inject
from pydantic import Field

from dashulya.ai.message_format import MessageFormatter, external_input
from dashulya.domain.memory import MemoryBackend


@tool
@inject
async def search_memory(
    query: Annotated[
        str,
        Field(
            description="Semantic/keyword query about earlier messages in this group, preferably the full request in its original language. Supply query or memory_id."
        ),
    ] = "",
    memory_id: Annotated[
        str | None,
        Field(
            description="Exact memory/source ID returned by an earlier search, for retrieving that source instead of running a new semantic query."
        ),
    ] = None,
    author_id: Annotated[
        int | None,
        Field(
            description="Numeric Telegram sender ID from observed messages or a chat_history SQL author lookup; narrows primary sources."
        ),
    ] = None,
    after: Annotated[
        datetime | None,
        Field(
            description="Inclusive original send-time lower bound as ISO 8601 with an explicit timezone offset."
        ),
    ] = None,
    before: Annotated[
        datetime | None,
        Field(
            description="Inclusive original send-time upper bound as ISO 8601 with an explicit timezone offset."
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="Maximum primary search hits, 1-8. Results are bounded recall, not an exact count or proof of absence."
        ),
    ] = 8,
    *,
    memory: FromDishka[MemoryBackend],
    formatter: FromDishka[MessageFormatter],
    user_id: Annotated[int | None, Variable(default=None)],
) -> str:
    """Recall this group's saved messages by meaning/keywords or a returned memory_id.

    The current group is bound to the trusted session; never supply a chat selector.
    Hits distinguish primary sources from supporting reply/nearby
    context, which may have other authors/dates inside the authorized snapshot.
    Check coverage and pending blocks. Inspect full sources and images with
    chat_history(sql=["SELECT * FROM messages WHERE source_id = 'returned source ID' LIMIT 1"]).
    Failures return an error string, not an empty result. Correct invalid arguments;
    for an unavailable recall backend, try chat_history for canonical originals.

    Examples (replace illustrative source IDs with actual returned memory IDs):
    - Case 1: Recall a previous decision by meaning and keywords.
      {"query": "What did we decide about image caching?"}
    - Case 2: Find discussions of a subject during a fixed period.
      {"query": "deployment checklist", "after": "2026-10-01T00:00:00+03:00", "before": "2026-11-01T00:00:00+03:00", "limit": 3}
    - Case 3: Read a source already identified by an earlier search.
      {"memory_id": "source:0123456789abcdef01234567:20998"}
    - Case 4: Recall one participant's proposals by their observed sender ID.
      {"query": "What changes did this participant propose?", "author_id": 42, "limit": 5}
    """
    result = await memory.search(
        query,
        memory_id=memory_id,
        author_id=author_id,
        after=after.isoformat() if after is not None else None,
        before=before.isoformat() if before is not None else None,
        limit=limit,
    )
    return external_input(
        json.dumps(formatter.result(result, interlocutor_id=user_id), ensure_ascii=False),
        "memory",
    )
