"""Port for the persistent sales-notification read model (G3-r6 / ISSUE-G4-03).

Notification identity is the decimal-string ``lead_id``; the read state lives
in PostgreSQL (``sales_notification_reads``) so unread counts survive reload
and a second device. Raw phones never enter this surface: rows carry the
masked phone projection only, derived in SQL/Python from the lead row.

The repository protocol is intentionally narrow (assigned-lead reads +
idempotent read-marking) so a fake can implement it exactly and the PG
adapter owns every SQL predicate.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class NotificationLeadRow:
    """One currently-assigned lead in notification projection (masked)."""

    lead_id: int
    project_key: str | None
    masked_phone: str | None
    display_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class ReadStateRow:
    lead_id: int
    read_at: datetime


class SalesNotificationsRepository(Protocol):
    async def list_assigned_notification_leads(
        self, *, sales_id: int, limit: int, cursor_created_at: datetime | None,
        cursor_lead_id: int | None,
    ) -> list[NotificationLeadRow]:
        """Assigned leads newest-first (created_at DESC, lead_id DESC)."""
        ...

    async def count_assigned_leads(self, *, sales_id: int) -> int: ...

    async def count_unread_leads(self, *, sales_id: int) -> int: ...

    async def get_read_states(self, *, sales_id: int, lead_ids: list[int]) -> dict[int, datetime]:
        """read_at per requested lead; absent keys are unread."""
        ...

    async def mark_read(self, *, sales_id: int, lead_ids: list[int]) -> list[int]:
        """Idempotent all-or-none insert; returns the ids converged as read."""
        ...

    async def mark_read_through(self, *, sales_id: int, through: datetime) -> list[int]:
        """Mark every assigned lead created at or before ``through`` read."""
        ...


class AssignedLeadsRepository(Protocol):
    """Narrow existence/assignment seam the CRM transcript route re-checks."""

    async def get_lead_assignments(self, lead_ids: list[int]) -> dict[int, int | None]:
        """lead_id -> assigned_sales_id for existing leads; absent = unknown."""
        ...


# --- Opaque stable cursor -----------------------------------------------------
# ``base64url("<epoch_micros>:<lead_id>")`` — the keyset pair of the sort
# order. Opaque to clients, stable across pages, and malformed values fail
# closed (400 at the route boundary) instead of silently restarting the scan.


def encode_notification_cursor(created_at: datetime, lead_id: int) -> str:
    payload = f"{int(created_at.timestamp() * 1_000_000)}:{lead_id}".encode()
    return base64.urlsafe_b64encode(payload).decode()


def decode_notification_cursor(value: str) -> tuple[datetime, int]:
    """Return (created_at, lead_id) or raise ValueError for malformed input."""
    try:
        raw = base64.urlsafe_b64decode(value.encode()).decode()
        micros_text, lead_text = raw.split(":", 1)
        micros = int(micros_text)
        lead_id = int(lead_text)
        created_at = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    except (ValueError, binascii.Error, UnicodeDecodeError, TypeError) as exc:
        raise ValueError("malformed notification cursor") from exc
    if lead_id < 1 or micros < 0:
        raise ValueError("malformed notification cursor")
    return created_at, lead_id


__all__ = [
    "NotificationLeadRow",
    "ReadStateRow",
    "SalesNotificationsRepository",
    "encode_notification_cursor",
    "decode_notification_cursor",
]
