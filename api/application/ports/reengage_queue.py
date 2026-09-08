"""Re-approach queue persistence port (story 9.4 / ISSUE-10).

The ReengageMatchWorkflow writes matched customers into a `reengage_queue`
collection so the CRM dashboard can surface "gợi ý tiếp cận lại" entries. The
store is write/read-by-admin-tooling only — the realtime chat pipeline never
touches it. Keeping the port narrow (save + attempt counts) lets the cap
enforcement stay in the workflow step where it is testable without Firestore.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReengageQueueEntry:
    """One matched re-approach suggestion for a previously-rejected customer.

    `attempt_count` is the number of queue entries this customer ALREADY has
    (including the one being written) so the per-customer reminder cap can be
    audited from the document itself.
    """

    customer_id: str
    project_key: str
    similarity_score: float
    rejection_reason: str | None
    budget_vnd: int | None
    attempt_count: int


class ReengageQueueNotConfiguredError(Exception):
    """Raised at wiring time when the queue binding is on but config is missing."""


class ReengageQueueStore(Protocol):
    """Persistence contract for the re-approach queue (Firestore today)."""

    async def save_queue_entries(self, entries: Sequence[ReengageQueueEntry]) -> None: ...

    async def load_attempt_counts_by_customer_id(self) -> dict[str, int]: ...

    async def cancel_queue_entries_for_customer(self, customer_id: str) -> None:
        """Remove every queued re-approach suggestion for one customer.

        Called when a customer withdraws marketing consent so the dashboard
        never surfaces "gợi ý tiếp cận lại" for someone who opted out. The
        store is a suggestion list only — a failure to cancel must not fail
        the withdrawal itself (the PG consent gate already re-filters).
        """
