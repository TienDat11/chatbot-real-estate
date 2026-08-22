"""One-way PG→Firestore lead mirror port (hybrid D1, frozen decision).

PG stays the source of truth for lead assignment; the Firestore document is a
write-only mirror consumed by the mobile/sales realtime clients. The BE is the
only writer, so the port exposes upsert/remove/health — no reads. Application
code depends only on this Protocol; a future transport swap (e.g. WebSocket
fan-out) replaces one adapter and one factory branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LeadMirrorDocument:
    """Denormalized lead snapshot mirrored to Firestore for realtime clients.

    `customer_id` is an HMAC-SHA256 digest of the lead phone, computed by BE
    code only — the raw phone never leaves the backend.
    `updated_at` is set by the adapter (ISO-8601) so the writer stamps a single
    consistent timestamp rather than trusting caller clocks.
    """

    customer_id: str
    project_key: str
    lead_status: str
    display_name: str | None
    masked_phone: str | None
    assigned_sales_firebase_uid: str | None
    consent_service: bool
    consent_marketing: bool
    consent_recorded_at: str | None
    last_customer_message_at: str | None
    updated_at: str
    # CRM-facing fields (story 9.3): the realtime table filters rejected
    # customers and reengage windows straight off Firestore, so the mirror
    # must carry them; defaulted so older constructions stay source-compatible.
    rejection_reason: str | None = None
    reengage_at: str | None = None
    marketing_withdrawn_at: str | None = None


class RealtimeLeadMirror(Protocol):
    """Writes lead documents into the realtime store (Firestore today)."""

    async def upsert_lead_mirror(self, *, customer_id: str, document: LeadMirrorDocument) -> None: ...
    async def remove_lead_mirror(self, customer_id: str) -> None: ...
    async def health_check(self) -> bool: ...


class RealtimeMirrorNotConfiguredError(Exception):
    """Raised at wiring time when the realtime binding is on but its config is missing."""


async def get_realtime_lead_mirror() -> RealtimeLeadMirror:
    from api.infrastructure.dependencies import get_realtime_lead_mirror as _di_factory

    # The DI factory owns the binding dispatch (off → Noop, firestore → REST).
    return await _di_factory()
