"""Customer-facing lead submission API (Epic 5/6 Story 6.4 + story 10.1).

Secure wave additions (spec §5.5/§9): one-time anonymous-quota bonus after a
committed lead, plus per-IP and per-phone spam brakes ahead of persistence.
"""

from __future__ import annotations

import ipaddress
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.application.services.anon_identity import (
    RATE_LIMIT_KIND_LEAD,
    AnonymousIdentityService,
)
from api.application.services.lead_mirror_service import (
    compute_customer_id,
    sync_lead_mirror_after_commit,
)
from api.application.services.lead_service import (
    LEAD_PHONE_COOLDOWN_SECONDS_DEFAULT,
    PhoneCooldownError,
    create_customer_lead,
    find_recent_lead_within_phone_cooldown,
    grant_post_lead_bonus_turns,
    normalize_phone,
    validate_phone,
)
from api.application.services.project_scope import (
    ProjectScopeError,
    validate_project_key,
)
from api.application.services.quota_service import QuotaRecordStore, check_ip_limit
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import LeadRepository, LeadRow, get_lead_repository
from api.infrastructure.ports.realtime_mirror import (
    RealtimeLeadMirror,
    get_realtime_lead_mirror,
)
from api.interfaces.api.anon_routes import get_anonymous_identity_service, get_client_ip_address

router = APIRouter(prefix="/api", tags=["lead"])
logger = logging.getLogger("api.lead")

# Sales CRM lead board — the screen the service worker opens on push click.
SALES_LEADS_URL = "/sales/leads"


class LeadSubmitRequest(BaseModel):
    # G1: project_key is REQUIRED — the lead's project is read from the request,
    # never guessed from the session (session_id is optional and may not resolve).
    project_key: str
    session_id: str | None = Field(default=None, max_length=128)
    # D7: anonymous persistent device id (UUID v4) — PII once paired with phone.
    device_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=50)
    phone: str = Field(..., min_length=9, max_length=20)
    consent: bool
    note: str | None = Field(default=None, max_length=200)
    budget_vnd: int | None = Field(default=None, ge=0)
    # Signed anonymous identity (spec §5.5). Optional and verified AFTER the
    # lead commits: an invalid token never fails the submission, it only
    # forgoes the bonus turns.
    anon_token: str | None = Field(default=None, max_length=512)

    @field_validator("project_key")
    @classmethod
    def project_key_is_valid(cls, value: str) -> str:
        try:
            validate_project_key(value)
        except ProjectScopeError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("phone")
    @classmethod
    def phone_is_vietnamese(cls, value: str) -> str:
        value = normalize_phone(value)
        if not validate_phone(value):
            raise ValueError("Số điện thoại chưa đúng định dạng Việt Nam")
        return value

    @field_validator("name", "note")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class LeadSubmitResponse(BaseModel):
    lead_id: int
    will_call_within_minutes: int = 5
    # §5.5 backward-compatible extension: turns granted on this submission's
    # one-time bonus; 0 without a valid identity or on a repeat grant (R3).
    quota_bonus_granted: int = 0


def get_lead_ip_request_limit() -> int:
    return get_settings().ip_rate_limit_lead_max_requests


def get_lead_ip_window_seconds() -> int:
    return get_settings().ip_rate_limit_window_seconds


def get_phone_cooldown_seconds() -> int:
    # Env knob instead of a Settings field keeps this brake tunable per
    # deployment while config.py stays owned by the identity wave issue.
    raw_cooldown_seconds = os.getenv("LEAD_PHONE_COOLDOWN_SECONDS")
    if raw_cooldown_seconds:
        return int(raw_cooldown_seconds)
    return LEAD_PHONE_COOLDOWN_SECONDS_DEFAULT


def get_quota_storage() -> QuotaRecordStore | None:
    # None defers to quota_service's late-bound Postgres adapter; tests inject
    # an in-memory store here instead of standing up a database.
    return None


async def dispatch_new_lead_push(lead: LeadRow) -> None:
    """Post-commit, budget-bounded sales push; failures are only logged.

    Runs as a FastAPI background task so FCM latency and outages never ride
    on the customer-facing lead response.
    """
    if lead.assigned_sales_id is None:
        return
    try:
        from api.infrastructure.dependencies import get_fcm_notification_service  # noqa: PLC0415

        await get_fcm_notification_service().notify_sales(
            sales_id=lead.assigned_sales_id,
            title="Lead mới",
            body=lead.name or "Có lead mới được gán cho bạn.",
            data={"type": "new_lead", "lead_id": str(lead.id), "url": SALES_LEADS_URL},
        )
    except Exception:  # noqa: BLE001 — push must never fail the committed lead
        logger.warning("new lead sales push failed for lead_id=%s", lead.id, exc_info=True)


def _session_device_claim_matches(
    session_device_id: str | None,
    header_device_id: str | None,
    payload_device_id: str | None,
) -> bool:
    """Accept ownership from either anonymous client transport."""
    return session_device_id == header_device_id or session_device_id == payload_device_id


def _normalized_client_ip_or_none(raw_ip_address: str | None) -> str | None:
    # The ip_rate_limit column is INET, so non-IP strings (hostname proxies,
    # the "unknown" fallback) would 500 every submission inside asyncpg. Such
    # deployments degrade to cooldown-only protection instead of failing.
    try:
        return str(ipaddress.ip_address(raw_ip_address))
    except (TypeError, ValueError):
        return None


@router.post("/lead", response_model=LeadSubmitResponse, status_code=201)
async def submit_lead(
    payload: LeadSubmitRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    repo: LeadRepository = Depends(get_lead_repository),  # noqa: B008
    mirror: RealtimeLeadMirror = Depends(get_realtime_lead_mirror),  # noqa: B008
    ip_request_limit: int = Depends(get_lead_ip_request_limit),
    ip_window_seconds: int = Depends(get_lead_ip_window_seconds),
    phone_cooldown_seconds: int = Depends(get_phone_cooldown_seconds),
    quota_storage: QuotaRecordStore | None = Depends(get_quota_storage),  # noqa: B008
    identity_service: AnonymousIdentityService = Depends(get_anonymous_identity_service),  # noqa: B008
    x_anon_token: str | None = Header(default=None, alias="X-Anon-Token"),
) -> LeadSubmitResponse:
    """Validate consent and phone, persist the lead, and assign a sales owner."""
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Consent is required")
    client_ip_address = _normalized_client_ip_or_none(get_client_ip_address(request))
    if client_ip_address is not None:
        ip_allowed = await check_ip_limit(
            client_ip_address,
            RATE_LIMIT_KIND_LEAD,
            ip_request_limit,
            ip_window_seconds,
            storage=quota_storage,
        )
        if not ip_allowed:
            raise HTTPException(
                status_code=429, detail="Too many leads submitted from this address"
            )
    if not hasattr(repo, "create_lead_if_phone_available"):
        recent_duplicate_lead = await find_recent_lead_within_phone_cooldown(
            repo, phone=payload.phone, cooldown_seconds=phone_cooldown_seconds
        )
        if recent_duplicate_lead is not None:
            raise HTTPException(
                status_code=429,
                detail="A lead with this phone number was submitted recently",
            )
    # Identity is verified BEFORE persistence so the committed row can carry
    # the recipient key for server-side push resolution. An invalid token
    # degrades to the HMAC phone digest (same derivation as the mirror) —
    # never the raw phone — and must not fail the submission (spec §5.5).
    claimed_anon_token = x_anon_token or payload.anon_token
    identity_claims = (
        identity_service.verify_token(claimed_anon_token) if claimed_anon_token else None
    )
    customer_identity = (
        identity_claims.subject
        if identity_claims is not None
        else compute_customer_id(payload.phone)
    )
    try:
        lead = await create_customer_lead(
            repo,
            session_id=payload.session_id,
            project_key=payload.project_key,
            device_id=payload.device_id,
            name=payload.name,
            phone=payload.phone,
            consent=payload.consent,
            note=payload.note,
            budget_vnd=payload.budget_vnd,
            phone_cooldown_seconds=phone_cooldown_seconds,
            customer_identity=customer_identity,
        )
    except PhoneCooldownError as exc:
        raise HTTPException(
            status_code=429,
            detail="A lead with this phone number was submitted recently",
        ) from exc
    # Story 9.2: PG is committed at this point — dual-write the Firestore
    # mirror as a pure side effect. Failures are flagged on the row and
    # retried by the reconciliation sweep, never surfaced to the customer.
    await sync_lead_mirror_after_commit(lead, repo=repo, mirror=mirror)
    if lead.assigned_sales_id is not None:
        # Push runs after the response is sent: FCM latency must never ride on
        # the customer-facing submission, and a delivery failure (the service
        # swallows it under a bounded budget) cannot fail the committed lead.
        background_tasks.add_task(dispatch_new_lead_push, lead)
    if payload.session_id:
        from api.infrastructure.adapters.postgres_chat_history import (
            repository as chat_history_repository,  # noqa: PLC0415
        )

        # Identity may arrive on either transport: the X-Anon-Token header or
        # the body anon_token field (the FE lead submit ships the signed token
        # in the body for the quota bonus, not as a header). An invalid token
        # degrades to None and the device claim can still authorize handoff.
        identity_key = identity_claims.subject if identity_claims is not None else None
        device_claim = x_device_id or payload.device_id
        try:
            # Anonymous handoff is fail-closed: the durable session must match
            # at least one verifiable ownership claim (signed identity OR
            # device id) plus the project. A bare session id never links.
            session = (
                await chat_history_repository.get_session_for_handoff(
                    session_id=payload.session_id,
                    project_key=payload.project_key,
                    device_id=device_claim,
                    identity_key=identity_key,
                )
                if device_claim or identity_key
                else None
            )
        except Exception:
            # Lead persistence must not depend on the optional history migration.
            session = None
        device_claims_consistent = not (
            x_device_id and payload.device_id and x_device_id != payload.device_id
        )
        if (
            session is not None
            and device_claims_consistent
            and _session_device_claim_matches(session.device_id, x_device_id, payload.device_id)
        ):
            try:
                await chat_history_repository.mark_handed_off(
                    session_id=payload.session_id, lead_id=lead.id
                )
            except Exception:
                # Handoff metadata is best-effort after the committed lead write;
                # the transcript read path self-heals from the lead's own
                # committed session claims, so this never blocks the customer.
                pass
    # §7: the bonus fires only after create + mirror succeeded, so a rejected
    # or failed submission never pays out turns.
    return LeadSubmitResponse(
        lead_id=lead.id,
        quota_bonus_granted=await grant_post_lead_bonus_turns(
            payload.anon_token,
            project_key=payload.project_key,
            quota_storage=quota_storage,
        ),
    )
