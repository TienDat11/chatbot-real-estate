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
    """Deterministic HMAC-SHA256 digest of the phone — the CUSTOMER identity.

    Keyed by ``lead_mirror_hmac_secret`` so deployments can pick their own
    secret; the digest must stay stable once written or Firestore documents
    orphan behind a new customer_id. The raw phone never leaves the backend.

    Since ADR-0004 this digest is a customer-scoped FIELD (grouping a
    person's leads), no longer the mirror document id.
    """
    secret = get_settings().lead_mirror_hmac_secret.encode("utf-8")
    return hmac.new(secret, phone.encode("utf-8"), hashlib.sha256).hexdigest()


# Domain-separation prefix so the per-lead document digest can never collide
# with — or be mistaken for — the phone-based customer digest (ADR-0004).
_LEAD_DOCUMENT_ID_MESSAGE_PREFIX = "lead-doc-id:"


def compute_lead_document_id(lead_id: int) -> str:
    """Opaque per-lead mirror document key (ADR-0004): HMAC of the PG lead id.

    Deterministic (re-pushes are idempotent), unique per lead, stable for the
    row's lifetime, and free of PII or lead-count side channels. Deriving it
    from the PG id — never the phone — is what ends the same-customer
    overwrite: two leads of one phone are two distinct documents.
    """
    secret = get_settings().lead_mirror_hmac_secret.encode("utf-8")
    message = f"{_LEAD_DOCUMENT_ID_MESSAGE_PREFIX}{lead_id}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _sales_firebase_uid_for_mirror(sales: SalesRow | None) -> str | None:
    """Return the mapped Firebase uid, warning for each legacy fallback."""
    if sales is None:
        return None
    if sales.firebase_uid is not None:
        return sales.firebase_uid
    logger.warning(
        "legacy sales access_key fallback used for lead mirror",
        extra={"metric": "sales_lead_mirror_legacy_access_key_fallback", "sales_id": sales.id},
    )
    return sales.access_key


def build_lead_mirror_document(lead: LeadRow, *, sales: SalesRow | None) -> LeadMirrorDocument:
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
        # The numeric PG id travels as a FIELD (integerValue): stream consumers
        # need it for the integer-only CRM routes because the opaque document
        # id no longer carries any addressable identity (ADR-0004).
        lead_id=lead.id,
        project_key=lead.project_key or "",
        created_at=lead.created_at.isoformat(),
        lead_status=lead.status,
        display_name=lead.name,
        masked_phone=mask_phone(lead.phone),
        # The realtime clients isolate by Firebase auth uid, so the mapped
        # provisioning uid (issue 4F) wins; pre-Firebase rows fall back to the
        # legacy access_key==uid equality (story 8.3 mapping). Writing a stale
        # access key here would orphan the lead from its owner in Firestore.
        assigned_sales_firebase_uid=_sales_firebase_uid_for_mirror(sales),
        consent_service=(
            lead.consent_service if lead.consent_service is not None else lead.consent
        ),
        consent_marketing=(lead.consent_marketing if lead.consent_marketing is not None else False),
        consent_recorded_at=(lead.consent_at.isoformat() if lead.consent_at is not None else None),
        # The chat transcript timestamp is not persisted on the lead row yet.
        last_customer_message_at=None,
        rejection_reason=lead.rejection_reason,
        reengage_at=(lead.reengage_at.isoformat() if lead.reengage_at is not None else None),
        marketing_withdrawn_at=(
            lead.marketing_withdrawn_at.isoformat()
            if lead.marketing_withdrawn_at is not None
            else None
        ),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


async def sync_lead_mirror_after_commit(
    lead: LeadRow,
    *,
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
) -> str:
    """Best-effort dual-write of one committed lead; never raises.

    Idempotent by construction: the document id derives from the immutable PG
    lead id (ADR-0004), and the mirror adapter's PATCH overwrites the single
    per-lead document. Returns the final mirror status ('done' | 'failed')
    so callers and tests can assert the outcome without parsing logs.
    """
    try:
        assigned_sales = (
            await repo.get_sales_by_id(lead.assigned_sales_id)
            if lead.assigned_sales_id is not None
            else None
        )
        document = build_lead_mirror_document(lead, sales=assigned_sales)
        document_id = compute_lead_document_id(lead.id)
        await mirror.upsert_lead_mirror(document_id=document_id, document=document)
        await _remove_legacy_customer_keyed_mirror(mirror, document, document_id)
        await repo.set_lead_mirror_status(lead.id, mirror_status=MIRROR_STATUS_DONE)
        return MIRROR_STATUS_DONE
    except Exception:  # noqa: BLE001 — the mirror must never fail the lead flow
        logger.warning("lead mirror upsert failed for lead_id=%s", lead.id, exc_info=True)
    try:
        await repo.set_lead_mirror_status(lead.id, mirror_status=MIRROR_STATUS_FAILED)
    except Exception:  # noqa: BLE001 — flagging is best-effort too
        logger.warning("lead mirror status flag failed for lead_id=%s", lead.id, exc_info=True)
    return MIRROR_STATUS_FAILED


async def reconcile_lead_mirrors(
    *,
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
    stale_before: datetime,
    limit: int = 100,
) -> dict[str, int]:
    """Retry only stale/failed committed leads and report convergence counts."""
    if limit < 1:
        raise ValueError("limit must be positive")
    leads = await repo.list_stale_mirror_leads(stale_before=stale_before, limit=limit)
    counts = {MIRROR_STATUS_DONE: 0, MIRROR_STATUS_FAILED: 0}
    for lead in leads:
        status = await sync_lead_mirror_after_commit(lead, repo=repo, mirror=mirror)
        counts[status] += 1
    return counts


async def _remove_legacy_customer_keyed_mirror(
    mirror: RealtimeLeadMirror,
    document: LeadMirrorDocument,
    document_id: str,
) -> None:
    """Best-effort erase of the pre-ADR-0004 document keyed by the phone HMAC.

    Pre-ADR-0004 mirrors wrote ``leads/{HMAC(phone)}``; every successful write
    now also deletes that legacy document so the backfill sweep converges to a
    single per-lead document. The cleanup must never fail the mirror: a 404
    (already gone) or a transport blip only logs.
    """
    legacy_document_id = document.customer_id
    if legacy_document_id == document_id:
        return
    try:
        await mirror.remove_lead_mirror(legacy_document_id)
    except Exception:  # noqa: BLE001 — cleanup is best-effort, never the mirror
        logger.warning(
            "legacy mirror cleanup failed for lead_id=%s", document.lead_id, exc_info=True
        )
