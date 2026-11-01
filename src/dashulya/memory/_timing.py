"""Content-free stage timings for one scoped memory search."""

import asyncio
import logging
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from dashulya.domain.memory import MemoryAccess

logger = logging.getLogger(__name__)
_metrics: ContextVar[dict[str, int] | None] = ContextVar(
    "memory_search_metrics", default=None
)


@contextmanager
def search_trace(access: MemoryAccess) -> Generator[dict[str, int]]:
    metrics: dict[str, int] = {}
    token = _metrics.set(metrics)
    started = perf_counter()
    outcome = "ok"
    try:
        yield metrics
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        _metrics.reset(token)
        logger.info(
            "Memory search completed",
            extra={
                "chat_id": access.chat_id,
                "message_id": access.before_id,
                "duration_ms": round((perf_counter() - started) * 1000),
                "outcome": outcome,
                "search_metrics": metrics,
            },
        )


@contextmanager
def search_stage(name: str) -> Generator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        if (metrics := _metrics.get()) is not None:
            key = name + "_ms"
            metrics[key] = metrics.get(key, 0) + round((perf_counter() - started) * 1000)
