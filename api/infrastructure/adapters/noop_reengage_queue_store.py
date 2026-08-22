"""No-op re-approach queue — used when the firebase binding is off (default)."""

from __future__ import annotations

from typing import Sequence

from api.application.ports.reengage_queue import ReengageQueueEntry, ReengageQueueStore


class NoopReengageQueueStore(ReengageQueueStore):
    """Swallows queue writes so callers need no binding awareness."""

    async def save_queue_entries(self, entries: Sequence[ReengageQueueEntry]) -> None:
        return None

    async def load_attempt_counts_by_customer_id(self) -> dict[str, int]:
        return {}
