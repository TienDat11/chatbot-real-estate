"""Server-authoritative anonymous quota gate for POST /query (secure wave Issue 2).

Contract placement (spec §4/§5): the gate evaluates AFTER project-scope
resolution — the 422 PROJECT_SCOPE answer stays byte-unchanged — and BEFORE
any pipeline leg runs, so a refused turn costs no LLM money and leaks no
partial answer. The turn itself is RESERVED ATOMICALLY here, before the
pipeline starts, through the conditional upsert in quota_service (spec §4 R2):
parallel submissions at the cap boundary can never overshoot the allowance.
A pipeline that later fails gets the reservation REFUNDED (spec §4 R1).

Store failure is FAIL-CLOSED for the anonymous machine: when the quota store
cannot be reached, the turn is refused (503) rather than served unlimited —
the gate exists to bound spend, so degradation must not open it. Admin
principals (verified Firebase ID token whose ``role`` claim is admin) bypass
quota entirely. Active-mapped sales principals — a verified Firebase token
whose ``role`` claim is sales backed by an active PG sales mapping (resolved
before this branch, 403/503 when missing/unavailable) — also bypass quota in
BOTH normal and ``answer_mode="training"`` chat: staff coaching must never hit
a customer-facing turn wall, and no quota row is written for them. Registered
customers receive 5 turns, and anonymous callers receive 3 turns plus one
5-turn lead bonus. Every other caller is keyed
by the signed anonymous identity token; a missing or tampered token self-heals
into a freshly minted (IP-rate-limited) identity instead of an error,
mirroring US-3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Union

from api.application.services.anon_identity import (
    RATE_LIMIT_KIND_MINT,
    RATE_LIMIT_KIND_QUERY,
    AnonymousIdentityService,
    RateLimitPort,
    get_rate_limit_port,
)
from api.application.services.quota_service import (
    QuotaExceeded,
    QuotaRecordStore,
    QuotaSnapshot,
    consume_turn,
)
from api.application.services.quota_service import (
    finalize_turn as finalize_reserved_turn,
)
from api.application.services.quota_service import refund_turn as refund_reserved_turn

logger = logging.getLogger("api.services.query_quota_gate")

# Spec §4 pins the anonymous allowance at 3 base turns. The env override
# arrives via Settings once the field lands there; getattr-with-default keeps
# this module decoupled from that future config edit.
DEFAULT_ANONYMOUS_BASE_TURN_CAP = 3
DEFAULT_CUSTOMER_BASE_TURN_CAP = 5

QUOTA_EXCEEDED_ERROR_CODE = "ANONYMOUS_QUOTA_EXCEEDED"
QUERY_IP_RATE_LIMITED_ERROR_CODE = "QUERY_IP_RATE_LIMITED"
IDENTITY_MINT_RATE_LIMITED_ERROR_CODE = "ANON_IDENTITY_RATE_LIMITED"
# Store failure = fail-closed for the anonymous machine: when quota cannot be
# enforced, the turn is refused instead of being served unlimited (the whole
# point of the gate is to bound spend, so degradation must not open it).
QUOTA_STORE_UNAVAILABLE_ERROR_CODE = "QUOTA_STORE_UNAVAILABLE"
SALES_MAPPING_MISSING_ERROR_CODE = "SALES_MAPPING_MISSING"
SALES_MAPPING_MISSING_MESSAGE = "Authenticated sales caller has no active sales mapping."
SALES_MAPPING_UNAVAILABLE_MESSAGE = "Sales authorization is temporarily unavailable."

# Customer-facing wall copy (spec §5.2); the number interpolates so a config
# change never desynchronizes the message from the enforced cap.
QUOTA_EXCEEDED_CUSTOMER_MESSAGE = (
    "Anh/chị đã dùng hết {base_turn_cap} lượt tư vấn miễn phí. "
    "Để lại số điện thoại để nhận tư vấn miễn phí nhé!"
)

QUERY_IP_RATE_LIMITED_MESSAGE = (
    "Too many chat requests from this address; please retry a bit later."
)
IDENTITY_MINT_RATE_LIMITED_MESSAGE = "Too many anonymous identities requested from this address."
QUOTA_STORE_UNAVAILABLE_MESSAGE = (
    "Anonymous quota is temporarily unavailable; please retry in a moment."
)

# Spec §4 R5: only these signed Firebase roles leave the anonymous machine.
BYPASS_ROLE_CLAIMS = frozenset({"sales", "admin"})


def _anonymous_quota_payload(snapshot: QuotaSnapshot) -> dict:
    """Map the internal snapshot onto the §5.1 wire shape (bonus_granted is
    the granted TURN COUNT per spec examples, not the internal boolean)."""
    return {
        "used_turns": snapshot.used_turns,
        "remaining_turns": snapshot.remaining_turns,
        "cap": snapshot.cap,
        "is_authenticated": False,
        "bonus_granted": snapshot.bonus_turns,
    }


@dataclass(frozen=True)
class AuthenticatedTurnContext:
    """Unlimited staff turns (spec §5.4); nothing is consumed or written.

    Shared by admin (role claim ``admin``) and active-mapped sales principals
    (role claim ``sales`` after `_resolve_verified_role` confirmed an active
    PG sales mapping). The payload exposes null quota so clients see an
    unlimited staff context, and the absence of ``identity_key``/``project_key``
    means the route never finalizes/refunds a reservation — sales chat leaves
    no quota row behind (no lead bonus, no CTA wall).
    """

    role_claim: str

    def quota_payload(self) -> dict:
        return {
            "used_turns": None,
            "remaining_turns": None,
            "cap": None,
            "is_authenticated": True,
            "bonus_granted": None,
        }


@dataclass(frozen=True)
class CustomerTurnContext:
    """Server-reserved finite quota for a verified Firebase identity.

    Registered customers use the 5-turn base policy; staff roles bypass quota
    via AuthenticatedTurnContext, so this shape is customer-only.
    """

    identity_key: str
    project_key: str
    reserved_snapshot: QuotaSnapshot
    effective_cap: int = DEFAULT_CUSTOMER_BASE_TURN_CAP

    def quota_payload(self) -> dict:
        payload = _anonymous_quota_payload(self.reserved_snapshot)
        payload["is_authenticated"] = True
        return payload


@dataclass(frozen=True)
class AnonymousTurnContext:
    """One turn already atomically reserved BEFORE the pipeline starts.

    ``reserved_snapshot`` is the post-reservation quota view; the reservation
    is released via refund_turn only when the pipeline fails (spec §4 R1).
    The reservation is scoped per project (spec §4 per-project quota), so a
    caller switching projects can never recycle an exhausted allowance.
    """

    identity_key: str
    project_key: str
    anon_token: str
    anon_token_is_newly_minted: bool
    reserved_snapshot: QuotaSnapshot

    def quota_payload(self) -> dict:
        return _anonymous_quota_payload(self.reserved_snapshot)


@dataclass(frozen=True)
class UnmanagedTurnContext:
    """Legacy passthrough when enforcement cannot run.

    Used when the signing secret is unset (dev/test stay bootable; production
    fails fast at Settings) or the quota store blipped. Serving unlimited is
    deliberate: the alternative — failing every chat because a secondary
    system is degraded — trades the product's core feature for
    defense-in-depth, and a real Postgres outage fails the request earlier
    anyway at project-scope resolution.
    """

    def quota_payload(self) -> dict:
        return {
            "used_turns": None,
            "remaining_turns": None,
            "cap": None,
            "is_authenticated": False,
            "bonus_granted": None,
        }


QueryTurnContext = Union[
    AuthenticatedTurnContext,
    CustomerTurnContext,
    AnonymousTurnContext,
    UnmanagedTurnContext,
]


class QueryQuotaBlockedError(Exception):
    """Pre-pipeline refusal carrying everything the 429/503 body needs."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        quota_payload: dict | None,
        lead_cta_required: bool,
        status_code: int = 429,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.message = message
        self.quota_payload = quota_payload
        self.lead_cta_required = lead_cta_required
        self.status_code = status_code

    def error_frame_data(self) -> dict:
        """Payload for the SSE ``error`` event (spec §5.3 shape)."""
        frame: dict = {"code": self.error_code, "message": self.message}
        if self.quota_payload is not None:
            frame["quota"] = self.quota_payload
        if self.lead_cta_required:
            frame["lead_cta"] = {"required": True}
        return frame


def build_quota_blocked_json_body(blocked: QueryQuotaBlockedError) -> dict:
    """Structured 429 body (spec §5.2 shape when the wall is quota)."""
    error_content: dict = {"code": blocked.error_code, "message": blocked.message}
    if blocked.quota_payload is not None:
        error_content["quota"] = blocked.quota_payload
    body: dict = {"ok": False, "error": error_content}
    if blocked.lead_cta_required:
        body["lead_cta"] = {"required": True}
    return body


def build_quota_exceeded_block(quota_payload: dict, base_turn_cap: int) -> QueryQuotaBlockedError:
    """Assemble the §5.2 exhaustion refusal, including mid-flight race losses."""
    return QueryQuotaBlockedError(
        error_code=QUOTA_EXCEEDED_ERROR_CODE,
        message=QUOTA_EXCEEDED_CUSTOMER_MESSAGE.format(base_turn_cap=base_turn_cap),
        quota_payload=quota_payload,
        lead_cta_required=True,
    )


class QueryQuotaGate:
    """Resolves the caller's turn context and enforces the anonymous allowance."""

    def __init__(
        self,
        *,
        signing_secret: str,
        ip_query_request_limit: int,
        ip_identity_mint_limit: int,
        ip_window_seconds: int,
        base_turn_cap: int = DEFAULT_ANONYMOUS_BASE_TURN_CAP,
        quota_store: QuotaRecordStore | None = None,
        rate_limiter: RateLimitPort | None = None,
    ) -> None:
        self._base_turn_cap = base_turn_cap
        self._ip_query_request_limit = ip_query_request_limit
        self._ip_identity_mint_limit = ip_identity_mint_limit
        self._ip_window_seconds = ip_window_seconds
        self._quota_store = quota_store
        self._rate_limiter = rate_limiter
        # An empty secret keeps dev/test bootable (production fails fast in
        # the Settings validator); enforcement then degrades to passthrough.
        self._identity_service = (
            AnonymousIdentityService(signing_secret) if signing_secret else None
        )

    @property
    def base_turn_cap(self) -> int:
        return self._base_turn_cap

    async def prepare_turn(
        self,
        *,
        presented_anon_token: str | None,
        bearer_id_token: str | None,
        client_ip_address: str,
        project_key: str,
    ) -> QueryTurnContext:
        """Decide who the caller is and whether the pipeline may run at all."""
        role_claim, firebase_uid = await self._resolve_verified_role(bearer_id_token)
        if role_claim in {"admin", "sales"}:
            # Staff bypass quota entirely. `_resolve_verified_role` already
            # enforced the active PG sales mapping for sales (403 when
            # missing, 503 when unavailable), so reaching here means an
            # authenticated, active, mapped sales principal — unlimited in
            # both normal and answer_mode="training" chat, with no quota row
            # written (unlike customers/anonymous).
            return AuthenticatedTurnContext(role_claim=role_claim)
        if firebase_uid and role_claim in {"customer", "user", "viewer"}:
            return await self._prepare_customer_turn(firebase_uid, project_key=project_key)
        if self._identity_service is None:
            return UnmanagedTurnContext()
        try:
            return await self._prepare_anonymous_turn(
                presented_anon_token, client_ip_address, project_key=project_key
            )
        except QueryQuotaBlockedError:
            raise
        except Exception:  # noqa: BLE001 — store failure is fail-CLOSED for anonymous
            logger.exception("quota store failure; refusing anonymous turn (fail-closed)")
            raise QueryQuotaBlockedError(
                error_code=QUOTA_STORE_UNAVAILABLE_ERROR_CODE,
                message=QUOTA_STORE_UNAVAILABLE_MESSAGE,
                quota_payload=None,
                lead_cta_required=False,
                status_code=503,
            ) from None

    async def finalize_turn(
        self, turn_context: AnonymousTurnContext | CustomerTurnContext
    ) -> bool:
        """Finalize a successful finite reservation without changing counters."""
        reservation_id = turn_context.reserved_snapshot.reservation_id
        if not reservation_id:
            return True
        try:
            return await finalize_reserved_turn(
                turn_context.identity_key,
                project_key=turn_context.project_key,
                reservation_id=reservation_id,
                storage=self._quota_store,
            )
        except Exception:  # noqa: BLE001 — preserve successful response
            logger.warning(
                "turn finalization failed; reservation remains for cleanup", exc_info=True
            )
            return False

    async def refund_turn(
        self, turn_context: AnonymousTurnContext | CustomerTurnContext
    ) -> dict | None:
        """Release a reserved turn after the pipeline failed (spec §4 R1).

        Returns the post-refund quota payload, or None when the refund could
        not be applied — the reservation is conservatively kept (fail-closed:
        a down store never grants an extra free turn).
        """
        try:
            outcome = await refund_reserved_turn(
                turn_context.identity_key,
                getattr(turn_context, "effective_cap", self._base_turn_cap),
                project_key=turn_context.project_key,
                reservation_id=turn_context.reserved_snapshot.reservation_id,
                storage=self._quota_store,
            )
        except Exception:  # noqa: BLE001 — never 500 the error path; keep the slot
            logger.warning(
                "turn refund failed after failed pipeline; reservation kept (fail-closed)",
                exc_info=True,
            )
            return None
        return _anonymous_quota_payload(outcome) if outcome is not None else None

    async def _prepare_customer_turn(
        self, firebase_uid: str, *, project_key: str
    ) -> CustomerTurnContext:
        reservation = await consume_turn(
            f"firebase:{firebase_uid}",
            DEFAULT_CUSTOMER_BASE_TURN_CAP,
            project_key=project_key,
            storage=self._quota_store,
        )
        if isinstance(reservation, QuotaExceeded):
            raise build_quota_exceeded_block(
                _anonymous_quota_payload(reservation.final_snapshot),
                DEFAULT_CUSTOMER_BASE_TURN_CAP,
            )
        return CustomerTurnContext(
            identity_key=f"firebase:{firebase_uid}",
            project_key=project_key,
            reserved_snapshot=reservation,
        )

    async def _prepare_anonymous_turn(
        self,
        presented_anon_token: str | None,
        client_ip_address: str,
        *,
        project_key: str,
    ) -> AnonymousTurnContext:
        assert self._identity_service is not None  # caller checked enforcement
        rate_limiter = self._rate_limiter or get_rate_limit_port()
        # Denied attempts still pay the bucket, so probing past the threshold
        # cannot rotate identities for free right at the brake (spec §9).
        query_allowed = await rate_limiter.check_rate_limit_allowed(
            client_ip_address,
            RATE_LIMIT_KIND_QUERY,
            self._ip_query_request_limit,
            self._ip_window_seconds,
        )
        if not query_allowed:
            raise QueryQuotaBlockedError(
                error_code=QUERY_IP_RATE_LIMITED_ERROR_CODE,
                message=QUERY_IP_RATE_LIMITED_MESSAGE,
                quota_payload=None,
                lead_cta_required=False,
            )

        verified_claims = (
            self._identity_service.verify_token(presented_anon_token)
            if presented_anon_token
            else None
        )
        if verified_claims is not None:
            identity_key = verified_claims.subject
            anon_token = presented_anon_token
            anon_token_is_newly_minted = False
        else:
            # Missing/tampered token self-heals (US-3): mint fresh, but the
            # mint pays the per-IP identity budget so clearing localStorage
            # cannot farm new allowances.
            mint_allowed = await rate_limiter.check_rate_limit_allowed(
                client_ip_address,
                RATE_LIMIT_KIND_MINT,
                self._ip_identity_mint_limit,
                self._ip_window_seconds,
            )
            if not mint_allowed:
                raise QueryQuotaBlockedError(
                    error_code=IDENTITY_MINT_RATE_LIMITED_ERROR_CODE,
                    message=IDENTITY_MINT_RATE_LIMITED_MESSAGE,
                    quota_payload=None,
                    lead_cta_required=False,
                )
            anon_token = self._identity_service.mint_token()
            reminted_claims = self._identity_service.verify_token(anon_token)
            if reminted_claims is None:  # pragma: no cover — HMAC roundtrip invariant
                raise RuntimeError("freshly minted anonymous token failed verification")
            identity_key = reminted_claims.subject
            anon_token_is_newly_minted = True

        reservation = await consume_turn(
            identity_key,
            self._base_turn_cap,
            project_key=project_key,
            storage=self._quota_store,
        )
        if isinstance(reservation, QuotaExceeded):
            # Atomic conditional upsert admitted no slot (spec §4 R2): the
            # allowance is exhausted, and a racing twin already spent the last
            # one — refused BEFORE any pipeline leg, so nothing is paid for and
            # no SSE/JSON token is ever emitted.
            raise build_quota_exceeded_block(
                _anonymous_quota_payload(reservation.final_snapshot),
                self._base_turn_cap,
            )
        return AnonymousTurnContext(
            identity_key=identity_key,
            project_key=project_key,
            anon_token=anon_token,
            anon_token_is_newly_minted=anon_token_is_newly_minted,
            reserved_snapshot=reservation,
        )

    async def _resolve_verified_role(
        self, bearer_id_token: str | None
    ) -> tuple[str | None, str | None]:
        """Resolve verified roles; never trust a sales claim without active PG mapping."""
        if not bearer_id_token:
            return None, None

        from api.infrastructure.dependencies import get_firebase_auth_verifier
        from api.interfaces.api.deps.admin import (
            SalesMappingMissing,
            SalesMappingUnavailable,
            resolve_assigned_sales_id,
        )

        try:
            verified_user = await get_firebase_auth_verifier().verify_id_token(bearer_id_token)
        except Exception:  # noqa: BLE001 — invalid bearer remains an ordinary caller
            logger.info("query bearer rejected; continuing as anonymous")
            return None, None

        role_claim = verified_user.role
        if role_claim != "sales":
            return role_claim, verified_user.firebase_uid

        try:
            await resolve_assigned_sales_id(verified_user.firebase_uid)
        except SalesMappingMissing as exc:
            logger.info("sales token has no active mapping; refusing elevated quota")
            raise QueryQuotaBlockedError(
                error_code=SALES_MAPPING_MISSING_ERROR_CODE,
                message=SALES_MAPPING_MISSING_MESSAGE,
                quota_payload=None,
                lead_cta_required=False,
                status_code=403,
            ) from exc
        except SalesMappingUnavailable as exc:
            raise QueryQuotaBlockedError(
                error_code=QUOTA_STORE_UNAVAILABLE_ERROR_CODE,
                message=SALES_MAPPING_UNAVAILABLE_MESSAGE,
                quota_payload=None,
                lead_cta_required=False,
                status_code=503,
            ) from exc
        return role_claim, verified_user.firebase_uid


def build_query_quota_gate() -> QueryQuotaGate:
    """Composition root used by the router; tests swap this factory out."""
    from api.infrastructure.config.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    return QueryQuotaGate(
        signing_secret=settings.anon_identity_secret,
        base_turn_cap=getattr(settings, "anonymous_base_turn_cap", DEFAULT_ANONYMOUS_BASE_TURN_CAP),
        ip_query_request_limit=settings.ip_rate_limit_query_max_requests,
        ip_identity_mint_limit=settings.ip_rate_limit_mint_max_requests,
        ip_window_seconds=settings.ip_rate_limit_window_seconds,
    )
