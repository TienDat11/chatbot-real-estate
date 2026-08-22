"""Port for the durable staff audit trail (story 9.5 / ISSUE-11).

Every BE-mediated staff mutation (phone reveal, CRM lead status change,
marketing-consent withdrawal, re-approach run trigger) records one entry.
The application layer depends on this port only — a Firestore `audit/`
collection or any other sink replaces one adapter, not the services.

PII rule: entries address customers by customer_id (HMAC of the phone) and
leads by numeric id. The raw phone, names, and free-form note text must
never be placed into an entry field or its detail payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Action vocabulary — stable identifiers consumed by future audit views.
STAFF_AUDIT_ACTION_PHONE_REVEALED = "customer_phone_revealed"
STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED = "lead_status_updated"
STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN = "marketing_consent_withdrawn"
STAFF_AUDIT_ACTION_REENGAGE_RUN_TRIGGERED = "reengage_run_triggered"


@dataclass(frozen=True)
class StaffAuditEntry:
    """One audited staff action, addressed without raw PII."""

    actor_firebase_uid: str
    actor_role: str
    action: str
    actor_sales_id: int | None = None
    customer_id: str | None = None
    lead_id: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class StaffAuditStore(Protocol):
    async def record_entry(self, entry: StaffAuditEntry) -> None: ...
