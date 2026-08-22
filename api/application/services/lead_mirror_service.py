"""PG-to-Firestore lead dual-write service (story 9.2, hybrid D1).

PG stays the source of truth for lead assignment; the Firestore document is
a one-way write-only mirror consumed by realtime clients. This service
builds the denormalized snapshot AFTER the PG write has committed and pushes
it best-effort: a mirror failure must never surface in the customer-facing
lead response — the row is flagged ``failed`` instead and the reconciliation
sweep (see ``lead_mirror_reconciliation``) retries it later.

Depends only on the leads read port and the RealtimeLeadMirror port — no
adapter imports — so a transport swap replaces one adapter, not this service.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from api.application.services.lead_service import mask_phone
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import LeadRepository, LeadRow, SalesRow
from api.infrastructure.ports.realtime_mirror import (
    LeadMirrorDocument,
    RealtimeLeadMirror,
)

logger = logging.getLogger("api.lead_mirror_service")

MIRROR_STATUS_PENDING = "pending"
MIRROR_STATUS_DONE = "done"
MIRROR_STATUS_FAILED = "failed"


def compute_customer_id(phone: str) -> str:
    """Deterministic HMAC-SHA256 digest of the phone — the mirror document key.

    Keyed by ``lead_mirror_hmac_secret`` so deployments can pick their own
    secret; the digest must stay stable once written or Firestore documents
    orphan behind a new customer_id. The raw phone never leaves the backend.
    """
    secret = get_settings().lead_mirror_hmac_secret.encode("utf-8")
    return hmac.new(secret, phone.encode("utf-8"), hashlib.sha256).hexdigest()


def build_lead_mirror_document(
    lead: LeadRow, *, sales: SalesRow | None
) -> LeadMirrorDocument:
    """Project one committed PG lead row into the realtime mirror snapshot.

    Consent split: ``consent_service`` / ``consent_marketing`` are the new 9.2
    columns; rows written before the migration (and in-memory fakes) still
    carry only the legacy single ``consent`` flag, which maps to service
    consent. Marketing consent is a separate opt-in and never inherited.
    ``updated_at`` is a caller-side stamp — the Firestore adapter restamps it
    with a single consistent clock.
    """
    return LeadMirrorDocument(
        customer_id=compute_customer_id(lead.phone),
        project_key=lead.project_key or "",
        lead_status=lead.status,
        display_name=lead.name,
        masked_phone=mask_phone(lead.phone),
        # sales.access_key doubles as the sales Firebase uid (story 8.3 mapping).
        assigned_sales_firebase_uid=sales.access_key if sales is not None else None,
        consent_service=(
            lead.consent_service if lead.consent_service is not None else lead.consent
        ),
        consent_marketing=(
            lead.consent_marketing if lead.consent_marketing is not None else False
        ),
        consent_recorded_at=(
            lead.consent_at.isoformat() if lead.consent_at is not None else None
        ),
        # The chat transcript timestamp is not persisted on the lead row yet.
        last_customer_message_at=None,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


async def sync_lead_mirror_after_commit(
    lead: LeadRow,
    *,
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
) -> str:
    """Best-effort dual-write of one committed lead; never raises.

    Idempotent by construction: the same phone yields the same customer_id,
    and the mirror adapter's PATCH overwrites the single document. Returns
    the final mirror status ('done' | 'failed') so callers and tests can
    assert the outcome without parsing logs.
    """
    try:
        assigned_sales = (
            await repo.get_sales_by_id(lead.assigned_sales_id)
            if lead.assigned_sales_id is not None
            else None
        )
        document = build_lead_mirror_document(lead, sales=assigned_sales)
        await mirror.upsert_lead_mirror(
            customer_id=document.customer_id, document=document
        )
        await repo.set_lead_mirror_status(
            lead.id, mirror_status=MIRROR_STATUS_DONE
        )
        return MIRROR_STATUS_DONE
    except Exception:  # noqa: BLE001 — the mirror must never fail the lead flow
        logger.warning("lead mirror upsert failed for lead_id=%s", lead.id, exc_info=True)
    try:
        await repo.set_lead_mirror_status(
            lead.id, mirror_status=MIRROR_STATUS_FAILED
        )
    except Exception:  # noqa: BLE001 — flagging is best-effort too
        logger.warning(
            "lead mirror status flag failed for lead_id=%s", lead.id, exc_info=True
        )
    return MIRROR_STATUS_FAILED
