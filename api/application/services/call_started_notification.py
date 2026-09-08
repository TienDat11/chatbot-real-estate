"""Exactly-once client call-started FCM dispatch (FR-33 / BE-CALL-NOTIFY-ONCE).

Every path that marks a lead ``called`` (CRM PATCH assigned->called, sales
action, broker PATCH) and the explicit tel-link ``/api/notifications/call-started``
endpoint converge here. Delivery policy: the DB CAS stamp
(``leads.call_notified_at``) is a CLAIM taken before any send; once claimed,
a failed FCM send is NOT retried and the stamp stays — at-most-once delivery,
never a duplicate push. Dispatch failures are logged and swallowed so they
can never fail the staff action that triggered the call.

The recipient is resolved SERVER-SIDE from the lead row (customer_identity +
assigned sales name); the payload carries no raw phone and no customer PII
beyond what the existing payload shape already had. The landing-url rule is
replicated here instead of imported from the interfaces layer (dependency
direction: application must not import interfaces).
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks

from api.application.ports.fcm_notifications import IDENTITY_TYPE_ANON_CUSTOMER
from api.application.services.fcm_notification_service import FcmNotificationService
from api.infrastructure.ports.leads import LeadRepository

logger = logging.getLogger("api.call_started_notification")

_CALL_STARTED_TITLE = "Cuộc gọi bắt đầu"
_NO_SALES_BODY = "Chuyên viên đang gọi cho bạn."


def _customer_landing_url(project_key: str | None) -> str:
    # Same rule the notifications route uses: the customer's own conversation
    # surface is the project chat page, falling back to the app root.
    return f"/project/{project_key}" if project_key else "/"


async def dispatch_call_started_once(
    repo: LeadRepository,
    service: FcmNotificationService,
    *,
    lead_id: int,
) -> bool:
    """Claim the call-started stamp, then dispatch the client push once.

    Returns True iff this call won the CAS claim AND a dispatch was attempted
    (the claim is still taken when the customer has no registered device or
    no resolvable identity — a second event on the same lead must never send,
    regardless of whether the first attempt could reach any device).
    """
    try:
        if not await repo.stamp_call_started(lead_id):
            # Lost the race, already notified, wrong status, or unknown lead:
            # never send a second push.
            return False
        lead = await repo.get_lead_by_id(lead_id)
        if lead is None:
            return False
        if lead.customer_identity is None:
            # Legacy row written before the identity column existed: nothing
            # to resolve, skip silently.
            return False
        sales = (
            await repo.get_sales_by_id(lead.assigned_sales_id)
            if lead.assigned_sales_id is not None
            else None
        )
        body = (
            f"Sales {sales.full_name} đang gọi tới bạn."
            if sales is not None
            else _NO_SALES_BODY
        )
        tokens = await service.resolve_identity_tokens(
            identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
            identity_key=lead.customer_identity,
        )
        if not tokens:
            return True
        await service.dispatch_to_tokens(
            tokens=tokens,
            title=_CALL_STARTED_TITLE,
            body=body,
            data={
                "type": "sales_call_started",
                "lead_id": str(lead.id),
                "url": _customer_landing_url(lead.project_key),
            },
        )
        return True
    except Exception:  # noqa: BLE001 — delivery must never fail the business event
        logger.warning(
            "call-started once-dispatch failed for lead_id=%s", lead_id, exc_info=True
        )
        return False


def queue_dispatch_call_started_once(
    background_tasks: BackgroundTasks,
    repo: LeadRepository,
    *,
    lead_id: int,
) -> None:
    """Queue the once-dispatch as a post-response background task.

    The FCM service is resolved lazily inside the task (same seam the
    sales-facing push uses) so the request path never pays for DI and tests
    can monkeypatch the dependency module.
    """

    async def _run() -> None:
        from api.infrastructure.dependencies import (  # noqa: PLC0415
            get_fcm_notification_service,
        )

        await dispatch_call_started_once(
            repo, get_fcm_notification_service(), lead_id=lead_id
        )

    background_tasks.add_task(_run)
