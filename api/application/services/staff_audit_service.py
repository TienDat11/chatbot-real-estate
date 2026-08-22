"""Audit use-case for staff mutations (story 9.5 / ISSUE-11).

Single entry point every staff-mutating service calls after its operation
succeeds. Recording is deliberately best-effort — the audit trail observes
the business operation and must never break it (same contract as the mirror
sync in story 9.2); a failed write is logged with full context so ops can
reconcile, and the caller learns the outcome through the returned flag.
"""

from __future__ import annotations

import logging
from typing import Any

from api.application.ports.staff_audit import StaffAuditEntry, StaffAuditStore
from api.interfaces.api.deps import AuthenticatedPrincipal

logger = logging.getLogger("api.staff_audit_service")


async def record_staff_action(
    audit_store: StaffAuditStore | None,
    *,
    principal: AuthenticatedPrincipal,
    action: str,
    customer_id: str | None = None,
    lead_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Persist one audited staff action; returns True when durably recorded.

    A missing store degrades to the stdout logger only (callers that were not
    wired with an adapter still leave an operational trace). Callers must pass
    detail payloads already scrubbed of raw PII — this function does not
    redact, it trusts the application layer to address customers by digest.
    """
    entry = StaffAuditEntry(
        actor_firebase_uid=principal.firebase_uid,
        actor_role=principal.role,
        actor_sales_id=principal.sales_id,
        action=action,
        customer_id=customer_id,
        lead_id=lead_id,
        detail=detail or {},
    )
    logger.info(
        "staff_audit action=%s actor_firebase_uid=%s actor_role=%s "
        "actor_sales_id=%s customer_id=%s lead_id=%s detail=%s",
        entry.action,
        entry.actor_firebase_uid,
        entry.actor_role,
        entry.actor_sales_id,
        entry.customer_id,
        entry.lead_id,
        entry.detail,
    )
    if audit_store is None:
        return False
    try:
        await audit_store.record_entry(entry)
    except Exception:  # noqa: BLE001 — auditing must never fail the mutation
        logger.error(
            "staff_audit WRITE FAILED action=%s actor_firebase_uid=%s "
            "customer_id=%s lead_id=%s",
            entry.action,
            entry.actor_firebase_uid,
            entry.customer_id,
            entry.lead_id,
            exc_info=True,
        )
        return False
    return True
