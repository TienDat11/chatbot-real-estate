"""PII-free audit projection for the phone-reveal action (G3-r6 §10.2).

The reveal is the highest-sensitivity staff read, so its audit record is
built through a CLOSED allowlist: a field that is not in
``PHONE_REVEAL_AUDIT_ALLOWED_FIELDS`` cannot enter the entry no matter what
a future caller passes in ``detail``. The raw phone can therefore never
become a secondary field of the audit trail; identity is the HMAC
``customer_id`` plus the numeric ``lead_ids``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from api.application.ports.staff_audit import StaffAuditEntry
from api.interfaces.api.deps import AuthenticatedPrincipal

logger = logging.getLogger("api.phone_reveal_audit")

# The normative field set of one reveal audit record (spec §10.2):
# action, actor_uid, actor_role, customer_id, lead_ids, occurred_at,
# correlation_id — plus the adapter's carrier columns (actor_sales_id).
PHONE_REVEAL_AUDIT_ALLOWED_FIELDS = frozenset(
    {"lead_ids", "occurred_at", "correlation_id"}
)


def build_phone_reveal_audit_entry(
    *,
    principal: AuthenticatedPrincipal,
    action: str,
    customer_id: str,
    lead_ids: list[int],
    correlation_id: str,
) -> StaffAuditEntry:
    """Construct the allowlisted audit entry for one successful reveal."""
    detail: dict[str, Any] = {
        "lead_ids": [int(lead_id) for lead_id in lead_ids],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
    }
    return StaffAuditEntry(
        actor_firebase_uid=principal.firebase_uid,
        actor_role=principal.role,
        actor_sales_id=principal.sales_id,
        action=action,
        customer_id=customer_id,
        lead_id=None,
        detail=detail,
    )


async def record_phone_reveal_entry(
    audit_store: Any | None, *, entry: StaffAuditEntry
) -> bool:
    """Best-effort durable write of one reveal entry (same contract as the
    generic staff recorder: an audit failure must never break the reveal
    that already completed, but it is logged with full context)."""
    logger.info(
        "phone_reveal_audit action=%s actor_firebase_uid=%s actor_role=%s "
        "actor_sales_id=%s customer_id=%s detail=%s",
        entry.action,
        entry.actor_firebase_uid,
        entry.actor_role,
        entry.actor_sales_id,
        entry.customer_id,
        entry.detail,
    )
    if audit_store is None:
        return False
    try:
        await audit_store.record_entry(entry)
    except Exception:  # noqa: BLE001 — auditing must never fail the reveal
        logger.error(
            "phone_reveal_audit WRITE FAILED actor_firebase_uid=%s customer_id=%s",
            entry.actor_firebase_uid,
            entry.customer_id,
            exc_info=True,
        )
        return False
    return True


__all__ = [
    "PHONE_REVEAL_AUDIT_ALLOWED_FIELDS",
    "build_phone_reveal_audit_entry",
    "record_phone_reveal_entry",
]
