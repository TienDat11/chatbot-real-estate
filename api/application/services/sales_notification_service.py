"""Sales-notification application service (G3-r6 / ISSUE-G4-03).

Owns the three notification invariants the routes must never inline:

1. **Assignment scoping** — every read/visibility decision runs against the
   CURRENT assignment (the repository joins ``leads.assigned_sales_id``), so
   a reassignment revokes notification visibility on the next request. An
   actor without a sales mapping (admin) resolves to an empty read model
   instead of a cross-tenant read.
2. **Read wins over stale replay** — marking is an idempotent upsert on the
   (sales_id, lead_id) PK; a later "unread" replay for the same lead is
   filtered out by the read-state join, never re-inflated by a second write.
   Partial batches are all-or-none because the repository applies them in
   one transaction.
3. **Contract caps** — limit 1..100, batch ids 1..100, ids must be decimal
   strings of positive integers; violations raise typed errors the route
   maps onto 413/422 without touching storage.

Empty/absent convergence: an empty list returns items=[], unread_count=0,
next_cursor=None; repeat mark calls return the converged count (200).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from api.application.ports.sales_notifications import (
    NotificationLeadRow,
    SalesNotificationsRepository,
    decode_notification_cursor,
    encode_notification_cursor,
)

NOTIFICATION_MAX_PAGE_SIZE = 100
NOTIFICATION_DEFAULT_PAGE_SIZE = 50
NOTIFICATION_MAX_BATCH_IDS = 100


class NotificationLimitOutOfRangeError(ValueError):
    """limit present but not a positive integer (contract 422 side)."""


class NotificationPageSizeCapError(ValueError):
    """limit exceeds the 1..100 contract cap (route maps to 413)."""


class NotificationBatchCapError(ValueError):
    """id batch exceeds 1..100 (route maps to 413)."""


class NotificationInvalidIdsError(ValueError):
    """ids not decimal-string positive integers (route maps to 422)."""


class NotificationMalformedCursorError(ValueError):
    """opaque cursor failed decode (route maps to 400)."""


@dataclass(frozen=True)
class NotificationItem:
    id: str
    lead_id: int
    project_key: str | None
    masked_phone: str | None
    display_name: str | None
    created_at: datetime
    read_at: datetime | None


@dataclass(frozen=True)
class NotificationListOutcome:
    items: list[NotificationItem]
    unread_count: int
    next_cursor: str | None


@dataclass(frozen=True)
class MarkReadOutcome:
    updated_ids: list[str]
    unread_count: int


@dataclass(frozen=True)
class MarkAllOutcome:
    read_through: datetime
    unread_count: int


def parse_notification_ids(raw_ids: list[str]) -> list[int]:
    """Validate the wire id set: decimal strings of positive integers."""
    if not raw_ids:
        raise NotificationInvalidIdsError("notification_ids must be non-empty")
    lead_ids: list[int] = []
    for raw in raw_ids:
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
            raise NotificationInvalidIdsError("notification_ids must be decimal strings")
        value = int(raw)
        if value < 1:
            raise NotificationInvalidIdsError("notification_ids must be positive")
        lead_ids.append(value)
    # Duplicate ids are legal idempotent noise; the cap counts DISTINCT ids,
    # so the ceiling is checked after de-duplication (normalize + cap).
    if len(set(lead_ids)) > NOTIFICATION_MAX_BATCH_IDS:
        raise NotificationBatchCapError("notification batch exceeds 100 ids")
    return sorted(set(lead_ids))


class SalesNotificationService:
    def __init__(self, repository: SalesNotificationsRepository) -> None:
        self.repository = repository

    async def list_notifications(
        self,
        *,
        sales_id: int | None,
        status: str,
        limit: int | None,
        cursor: str | None,
    ) -> NotificationListOutcome:
        if status not in {"unread", "all"}:
            raise NotificationInvalidIdsError("status must be unread or all")
        resolved_limit = NOTIFICATION_DEFAULT_PAGE_SIZE if limit is None else limit
        if resolved_limit < 1:
            raise NotificationLimitOutOfRangeError("limit must be >= 1")
        if resolved_limit > NOTIFICATION_MAX_PAGE_SIZE:
            raise NotificationPageSizeCapError("limit exceeds 100")
        cursor_created_at: datetime | None = None
        cursor_lead_id: int | None = None
        if cursor is not None:
            try:
                cursor_created_at, cursor_lead_id = decode_notification_cursor(cursor)
            except ValueError as exc:
                raise NotificationMalformedCursorError(str(exc)) from exc

        if sales_id is None:
            # Admin/other principals have no assignment: converge to the empty
            # read model (items [], count 0) — never a cross-sales read.
            return NotificationListOutcome(items=[], unread_count=0, next_cursor=None)

        # Fetch one extra row to decide next_cursor without a second query.
        rows = await self.repository.list_assigned_notification_leads(
            sales_id=sales_id,
            limit=resolved_limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_lead_id=cursor_lead_id,
        )
        unread_count = await self.repository.count_unread_leads(sales_id=sales_id)
        has_more = len(rows) > resolved_limit
        page = rows[:resolved_limit]
        read_states = await self.repository.get_read_states(
            sales_id=sales_id, lead_ids=[row.lead_id for row in page]
        )
        items: list[NotificationItem] = []
        for row in page:
            read_at = read_states.get(row.lead_id)
            if status == "unread" and read_at is not None:
                # Read-wins filter for the unread view: the assignment join is
                # the source of truth, the read row suppresses the replay.
                continue
            items.append(_to_item(row, read_at))
        next_cursor = None
        if has_more and page:
            next_cursor = encode_notification_cursor(page[-1].created_at, page[-1].lead_id)
        return NotificationListOutcome(
            items=items, unread_count=unread_count, next_cursor=next_cursor
        )

    async def mark_read(
        self, *, sales_id: int | None, notification_ids: list[str]
    ) -> MarkReadOutcome:
        lead_ids = parse_notification_ids(notification_ids)
        if sales_id is None:
            # Nothing can be marked without a sales mapping; the response
            # still converges to the caller's own empty unread count.
            return MarkReadOutcome(updated_ids=[], unread_count=0)
        # Idempotent upsert on (sales_id, lead_id): a repeat of the same batch
        # rewrites read_at atomically (ON CONFLICT), and read state for leads
        # the caller no longer owns is never consulted for visibility again —
        # visibility is assignment-gated at list time.
        updated = await self.repository.mark_read(sales_id=sales_id, lead_ids=lead_ids)
        unread_count = await self.repository.count_unread_leads(sales_id=sales_id)
        return MarkReadOutcome(
            updated_ids=[str(lead_id) for lead_id in updated], unread_count=unread_count
        )

    async def mark_all_through(
        self, *, sales_id: int | None, through: datetime
    ) -> MarkAllOutcome:
        if sales_id is None:
            return MarkAllOutcome(read_through=through, unread_count=0)
        await self.repository.mark_read_through(sales_id=sales_id, through=through)
        unread_count = await self.repository.count_unread_leads(sales_id=sales_id)
        return MarkAllOutcome(read_through=through, unread_count=unread_count)


def _to_item(row: NotificationLeadRow, read_at: datetime | None) -> NotificationItem:
    return NotificationItem(
        id=str(row.lead_id),
        lead_id=row.lead_id,
        project_key=row.project_key,
        masked_phone=row.masked_phone,
        display_name=row.display_name,
        created_at=row.created_at,
        read_at=read_at,
    )


__all__ = [
    "NOTIFICATION_DEFAULT_PAGE_SIZE",
    "NOTIFICATION_MAX_BATCH_IDS",
    "NOTIFICATION_MAX_PAGE_SIZE",
    "NotificationBatchCapError",
    "NotificationInvalidIdsError",
    "NotificationLeadRow",
    "NotificationLimitOutOfRangeError",
    "NotificationListOutcome",
    "NotificationMalformedCursorError",
    "NotificationPageSizeCapError",
    "NotificationItem",
    "SalesNotificationService",
    "MarkAllOutcome",
    "MarkReadOutcome",
    "parse_notification_ids",
]
