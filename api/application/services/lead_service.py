"""Lead routing and broker action orchestration."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.conv_state import mark_phone_given
from api.application.services.quota_service import QuotaRecordStore, grant_bonus
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import LeadRepository, LeadRow, SalesRow
from api.infrastructure.ports.realtime_mirror import RealtimeLeadMirror

logger = logging.getLogger("api.lead_service")

_PHONE_SEPARATORS = re.compile(r"[\s,.-]")
_PHONE_PATTERN = re.compile(r"^(0|\+84)(3[2-9]|5[5-9]|7[0-9]|8[1-9]|9[0-9])[0-9]{7}$")
LEAD_LOCK_MINUTES = 5


def normalize_phone(value: str) -> str:
    return _PHONE_SEPARATORS.sub("", value.strip())


def validate_phone(value: str) -> bool:
    return bool(_PHONE_PATTERN.fullmatch(normalize_phone(value)))


def mask_phone(value: str) -> str:
    value = normalize_phone(value)
    if len(value) < 7:
        return "***"
    return value[:4] + "***" + value[-3:]


def normalize_persisted_datetime(value: datetime) -> datetime:
    """Normalize persisted timestamps before application-side comparisons.

    PostgreSQL ``timestamptz`` values arrive aware while legacy rows may contain
    naive UTC values. Treating a naive value as UTC preserves its stored instant
    without applying the host machine's local timezone.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def choose_next_sales(
    active_sales: list[SalesRow],
    excluded_ids: list[int],
    *,
    preferred_firebase_uid: str | None = None,
) -> SalesRow | None:
    """Route LRU-first, with priority only as the tiebreaker.

    ``preferred_firebase_uid`` (env SALES_PREFERRED_FIREBASE_UID) is a narrow
    test/verification override: when it maps to an active, non-excluded sales
    that identity is returned immediately regardless of recency or priority.
    Unset/blank, unmapped or inactive preferences leave the standard LRU-first
    algorithm untouched, so production fairness is unchanged by default.
    """
    candidates = [
        sales for sales in active_sales if sales.id not in excluded_ids and sales.is_active
    ]
    if not candidates:
        return None
    if preferred_firebase_uid:
        preferred = next(
            (
                sales
                for sales in candidates
                if sales.is_active and sales.firebase_uid == preferred_firebase_uid
            ),
            None,
        )
        if preferred is not None:
            return preferred
    return min(
        candidates,
        key=lambda sales: (
            sales.last_assigned_at is not None,
            normalize_persisted_datetime(sales.last_assigned_at)
            if sales.last_assigned_at is not None
            else datetime.min.replace(tzinfo=timezone.utc),
            -sales.priority,
            sales.id,
        ),
    )


def _preferred_sales_firebase_uid() -> str | None:
    """Return the env-configured preferred sales Firebase uid, or ``None`` when
    blank/unset so the LRU-first algorithm is preserved unchanged."""
    value = (get_settings().sales_preferred_firebase_uid or "").strip()
    return value or None


async def _assign(
    repo: LeadRepository,
    lead_id: int,
    *,
    excluded_ids: list[int] | None = None,
    action: str = "assign",
    note: str | None = None,
    expected_assigned_sales_id: int | None = None,
    prelude_actions: tuple[tuple[str, int | None, str | None], ...] = (),
) -> tuple[SalesRow | None, LeadRow | None]:
    atomic_assign = getattr(repo, "assign_lead", None)
    preferred_uid = _preferred_sales_firebase_uid()
    if atomic_assign is not None:
        # One repository transaction owns candidate selection + mutation + log
        # insertion, so a concurrent decision for the same lead (row lock)
        # cannot produce a diverging assignment or a duplicate/stale log.
        decision = await atomic_assign(
            lead_id,
            excluded_sales_ids=list(excluded_ids or []),
            action=action,
            note=note or "LRU routing",
            expected_assigned_sales_id=expected_assigned_sales_id,
            preferred_firebase_uid=preferred_uid,
        )
        return decision.next_sales, decision.lead
    # Legacy fallback for callers/fakes without the atomic method: the guarded
    # update still prevents a former owner from stealing the lead, but the
    # candidate reads and log insertion are separate transactions.
    tried_ids = await repo.get_tried_sales_ids(lead_id)
    sales = choose_next_sales(
        await repo.list_active_sales(),
        list(set((excluded_ids or []) + tried_ids)),
        preferred_firebase_uid=preferred_uid,
    )
    if sales is None:
        update_kwargs: dict[str, Any] = {"status": "expired", "close": True}
        if expected_assigned_sales_id is not None:
            update_kwargs["expected_assigned_sales_id"] = expected_assigned_sales_id
        import inspect

        if "expected_assigned_sales_id" not in inspect.signature(repo.update_lead).parameters:
            update_kwargs.pop("expected_assigned_sales_id", None)
        lead = await repo.update_lead(lead_id, **update_kwargs)
        if lead:
            for pre_action, pre_sales_id, pre_note in prelude_actions:
                await repo.add_assignment_log(lead_id, pre_sales_id, pre_action, pre_note)
            await repo.add_assignment_log(lead_id, None, "expired", "[ESCALATED-ALL]")
        return None, lead

    update_kwargs: dict[str, Any] = {
        "status": "assigned",
        "assigned_sales_id": sales.id,
        "lock_expires_at": datetime.now(timezone.utc) + timedelta(minutes=LEAD_LOCK_MINUTES),
    }
    if expected_assigned_sales_id is not None:
        update_kwargs["expected_assigned_sales_id"] = expected_assigned_sales_id
    import inspect

    if "expected_assigned_sales_id" not in inspect.signature(repo.update_lead).parameters:
        update_kwargs.pop("expected_assigned_sales_id", None)
    lead = await repo.update_lead(lead_id, **update_kwargs)
    if lead:
        for pre_action, pre_sales_id, pre_note in prelude_actions:
            await repo.add_assignment_log(lead_id, pre_sales_id, pre_action, pre_note)
        await repo.add_assignment_log(lead_id, sales.id, action, note or "LRU routing")
    return sales, lead


async def create_customer_lead(
    repo: LeadRepository,
    *,
    session_id: str | None,
    project_key: str | None,
    device_id: str | None,
    name: str | None,
    phone: str,
    consent: bool,
    note: str | None,
    budget_vnd: int | None,
    phone_cooldown_seconds: int | None = None,
    customer_identity: str | None = None,
) -> LeadRow:
    if phone_cooldown_seconds is not None and hasattr(repo, "create_lead_if_phone_available"):
        lead = await repo.create_lead_if_phone_available(
            cooldown_seconds=phone_cooldown_seconds,
            session_id=session_id,
            project_key=project_key,
            device_id=device_id,
            name=name,
            phone=phone,
            consent=consent,
            note=note,
            budget_vnd=budget_vnd,
            customer_identity=customer_identity,
        )
        if lead is None:
            raise PhoneCooldownError
    else:
        lead = await repo.create_lead(
            session_id=session_id,
            project_key=project_key,
            device_id=device_id,
            name=name,
            phone=phone,
            consent=consent,
            note=note,
            budget_vnd=budget_vnd,
            customer_identity=customer_identity,
        )
    # Session state is in-memory only, so mark here instead of in the route to
    # keep every caller of this service consistent (plan §6.7). Anonymous
    # sessions get no marker: get_context would mint a throwaway key.
    # device_id prefixes the same cache key the chat context lives under (D7),
    # so the handoff flags gate the next CTA on the device's conversation.
    if session_id:
        mark_phone_given(session_id, device_id)
    _, assigned = await _assign(repo, lead.id)
    return assigned or lead


# Spam brake (spec §9): one lead per phone per day by default; the route reads
# LEAD_PHONE_COOLDOWN_SECONDS to tighten or loosen the window per deployment.
LEAD_PHONE_COOLDOWN_SECONDS_DEFAULT = 24 * 60 * 60


class PhoneCooldownError(RuntimeError):
    """Raised when a concurrent submission owns the phone cooldown window."""


async def find_recent_lead_within_phone_cooldown(
    repo: LeadRepository,
    *,
    phone: str,
    cooldown_seconds: int,
) -> LeadRow | None:
    """Return the newest lead this phone opened inside the cooldown window.

    The lookup keys on the HMAC customer_id digest — the exact derivation the
    Firestore mirror uses (story 9.2) — instead of a second raw-phone key
    space, so spam dedupe and mirroring always agree on what "same customer"
    means even if stored phone formatting ever diverges.
    """
    # Late import: lead_mirror_service already imports this module for
    # mask_phone, so a module-level import would close a circular chain.
    from api.application.services.lead_mirror_service import compute_customer_id  # noqa: PLC0415

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(cooldown_seconds, 0))
    matching_leads = await repo.get_leads_by_customer_id(compute_customer_id(phone))
    recent_leads = [
        lead for lead in matching_leads if normalize_persisted_datetime(lead.created_at) >= cutoff
    ]
    if not recent_leads:
        return None
    return max(recent_leads, key=lambda lead: normalize_persisted_datetime(lead.created_at))


async def grant_post_lead_bonus_turns(
    anon_token: str | None,
    *,
    project_key: str,
    quota_storage: QuotaRecordStore | None = None,
) -> int:
    """Grant the one-time anonymous-quota bonus after a committed lead.

    Returns the turns actually granted: 0 when no token was presented, when
    the token fails verification (silently — the lead itself already
    succeeded and must not 500 over an identity problem, spec §5.5), or when
    the identity already spent its one-time grant (spec §4 R3; idempotency
    lives in the atomic quota store, not in this wrapper).
    """
    if not anon_token:
        return 0
    settings = get_settings()
    if settings.anonymous_bonus_turns_after_lead < 1:
        return 0
    if not settings.anon_identity_secret:
        # No signing secret configured means tokens cannot be verified; treat
        # them as absent instead of failing a lead that already committed.
        return 0
    identity_claims = AnonymousIdentityService(settings.anon_identity_secret).verify_token(
        anon_token
    )
    if identity_claims is None:
        return 0
    granted = await grant_bonus(
        identity_claims.subject,
        settings.anonymous_bonus_turns_after_lead,
        project_key=project_key,
        storage=quota_storage,
    )
    return settings.anonymous_bonus_turns_after_lead if granted else 0


async def _sync_lead_mirror(
    repo: LeadRepository,
    mirror: RealtimeLeadMirror | None,
    lead: LeadRow | None,
) -> LeadRow | None:
    """Best-effort Firestore convergence after a PG broker-board mutation.

    Without this push the mirror keeps the PREVIOUS assignment/status forever
    (production bug: a no-answer reassignment updated PG only, so Firestore
    still showed the old sales). Late import: lead_mirror_service imports this
    module for mask_phone, so a module-level import would close a circular
    chain.
    """
    if mirror is None or lead is None:
        return lead
    from api.application.services.lead_mirror_service import (  # noqa: PLC0415
        sync_lead_mirror_after_commit,
    )

    await sync_lead_mirror_after_commit(lead, repo=repo, mirror=mirror)
    return lead


async def _update_owned_lead(
    repo: LeadRepository, lead_id: int, sales_id: int, **kwargs: Any
) -> LeadRow | None:
    """Use atomic owner predicate when the repository supports it."""
    import inspect

    update = repo.update_lead
    if "expected_assigned_sales_id" in inspect.signature(update).parameters:
        kwargs["expected_assigned_sales_id"] = sales_id
    return await update(lead_id, **kwargs)


async def handle_lead_action(
    repo: LeadRepository,
    sales: SalesRow,
    lead_id: int,
    action: str,
    note: str | None = None,
    *,
    mirror: RealtimeLeadMirror | None = None,
) -> LeadRow | None:
    lead = await repo.get_lead_by_id(lead_id)
    if not lead or lead.assigned_sales_id != sales.id:
        return None

    if action == "called":
        if lead.status != "assigned":
            return None
        updated = await _update_owned_lead(repo, lead.id, sales.id, status="called")
        if updated:
            await repo.add_assignment_log(updated.id, sales.id, "call", note)
        return await _sync_lead_mirror(repo, mirror, updated)

    if action == "no_answer":
        if lead.status not in ("assigned", "called"):
            return None
        atomic_assign = getattr(repo, "assign_lead", None)
        if atomic_assign is not None:
            # One transaction: the no_answer log, the ownership predicate, the
            # candidate selection and the reassignment mutation cannot diverge.
            # A former owner whose guarded update affects zero rows produces no
            # log at all.
            decision = await atomic_assign(
                lead.id,
                excluded_sales_ids=[sales.id],
                action="assign",
                note="Reassign after no_answer",
                expected_assigned_sales_id=sales.id,
                prelude_actions=(("no_answer", sales.id, note),),
                preferred_firebase_uid=_preferred_sales_firebase_uid(),
            )
            return await _sync_lead_mirror(repo, mirror, decision.lead)
        _, reassigned = await _assign(
            repo,
            lead.id,
            excluded_ids=[sales.id],
            action="assign",
            note="Reassign after no_answer",
            expected_assigned_sales_id=sales.id,
            prelude_actions=(("no_answer", sales.id, note),),
        )
        return await _sync_lead_mirror(repo, mirror, reassigned)

    if action == "callback":
        if lead.status not in ("assigned", "called"):
            return None
        updated = await _update_owned_lead(
            repo,
            lead.id,
            sales.id,
            status="callback",
            lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        if updated:
            await repo.add_assignment_log(updated.id, sales.id, "callback", note)
        return await _sync_lead_mirror(repo, mirror, updated)

    if action in ("booked", "lost"):
        if lead.status not in ("assigned", "called", "callback"):
            return None
        updated = await _update_owned_lead(repo, lead.id, sales.id, status=action, close=True)
        if updated:
            await repo.add_assignment_log(updated.id, sales.id, action, note)
        return await _sync_lead_mirror(repo, mirror, updated)

    return None


async def get_sales_dashboard(repo: LeadRepository, sales: SalesRow) -> dict[str, Any]:
    # Lazy import: lead_mirror_service imports mask_phone from this module,
    # so a module-level import here would be circular.
    from api.application.services.lead_mirror_service import compute_customer_id  # noqa: PLC0415

    leads = await repo.get_active_leads_for_sales(sales.id)
    stats = await repo.get_sales_stats(sales.id)
    return {
        "server_time": datetime.now().isoformat(),
        "leads": [
            {
                "lead_id": lead.id,
                # BE-CRM: opaque HMAC customer id so the assigned-lead list can
                # address the phone-reveal route without ever shipping the raw
                # number (PII minimality; additive to the wire shape).
                "customer_id": compute_customer_id(lead.phone) if lead.phone else None,
                "name": lead.name,
                "phone": mask_phone(lead.phone) if lead.phone else None,
                "note": lead.note,
                "budget_vnd": lead.budget_vnd,
                "created_at": lead.created_at.isoformat(),
                "lock_expires_at": lead.lock_expires_at.isoformat()
                if lead.lock_expires_at
                else None,
                "escal_count": lead.escal_count,
            }
            for lead in leads
        ],
        "stats": {
            "today": stats.today,
            "avg_answer_seconds": stats.avg_answer_seconds,
            "avg_answer_seconds_reason": stats.avg_answer_seconds_reason,
        },
    }
