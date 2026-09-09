"""Signed anonymous identity — server-minted HMAC-SHA256 chat tokens.

The anonymous quota (secure wave G2) is server-authoritative, so identity
cannot be a client-chosen device id: clearing localStorage must not reset
quota without paying the per-IP mint limit. Identity travels in a compact
signed token ``base64url(payload) + "." + base64url(HMAC-SHA256(payload))``
with payload ``{"sub": uuid4, "iat": epoch, "ver": 1}`` and no PII.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

ANONYMOUS_IDENTITY_TOKEN_VERSION = 1
ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_DAYS = 90
ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS = (
    ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_DAYS * 24 * 60 * 60
)
# Mint and verify may run on machines with drifting clocks; small skew is
# tolerated so a just-minted token never fails verification spuriously.
CLOCK_SKEW_TOLERANCE_SECONDS = 60

# Rate-limit kinds shared by every IP-braked endpoint (spec §6 ip_rate_limit).
RATE_LIMIT_KIND_MINT = "mint"
RATE_LIMIT_KIND_QUERY = "query"
RATE_LIMIT_KIND_LEAD = "lead"


@dataclass(frozen=True)
class AnonymousIdentityClaims:
    """Verified contents of an anonymous identity token; PII-free by design."""

    subject: str
    issued_at_epoch_seconds: int
    token_version: int


def _encode_base64url_segment(raw: bytes) -> str:
    # Padding stripped (JWT-style); decoding restores it, keeping tokens compact.
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_base64url_segment(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _serialize_claims_payload(claims: AnonymousIdentityClaims) -> bytes:
    # Canonical JSON (sorted keys, no whitespace) keeps signatures deterministic.
    return json.dumps(
        {
            "iat": claims.issued_at_epoch_seconds,
            "sub": claims.subject,
            "ver": claims.token_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _compute_signature_bytes(payload_bytes: bytes, signing_secret: str | bytes) -> bytes:
    secret_bytes = (
        signing_secret.encode("utf-8") if isinstance(signing_secret, str) else signing_secret
    )
    return hmac.new(secret_bytes, payload_bytes, hashlib.sha256).digest()


def mint_anonymous_identity_token(
    signing_secret: str | bytes,
    *,
    now_epoch_seconds: int | None = None,
    subject: str | None = None,
) -> str:
    """Create a fresh signed anonymous identity token."""
    issued_at_epoch_seconds = (
        now_epoch_seconds if now_epoch_seconds is not None else int(time.time())
    )
    claims = AnonymousIdentityClaims(
        subject=subject or str(uuid.uuid4()),
        issued_at_epoch_seconds=issued_at_epoch_seconds,
        token_version=ANONYMOUS_IDENTITY_TOKEN_VERSION,
    )
    payload_bytes = _serialize_claims_payload(claims)
    signature_bytes = _compute_signature_bytes(payload_bytes, signing_secret)
    return (
        f"{_encode_base64url_segment(payload_bytes)}.{_encode_base64url_segment(signature_bytes)}"
    )


def verify_anonymous_identity_token(
    token: str,
    signing_secret: str | bytes,
    *,
    now_epoch_seconds: int | None = None,
) -> AnonymousIdentityClaims | None:
    """Verify signature + freshness; None means "no usable identity" so callers
    can uniformly self-heal by minting a fresh (IP-rate-limited) token."""
    current_epoch_seconds = now_epoch_seconds if now_epoch_seconds is not None else int(time.time())
    try:
        payload_segment, signature_segment = token.split(".")
        payload_bytes = _decode_base64url_segment(payload_segment)
        provided_signature_bytes = _decode_base64url_segment(signature_segment)
    except (ValueError, TypeError):
        return None

    expected_signature_bytes = _compute_signature_bytes(payload_bytes, signing_secret)
    # Constant-time comparison so timing cannot leak the signature bytes.
    if not hmac.compare_digest(expected_signature_bytes, provided_signature_bytes):
        return None

    try:
        raw_claims = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_claims, dict):
        return None

    subject = raw_claims.get("sub")
    issued_at_epoch_seconds = raw_claims.get("iat")
    token_version = raw_claims.get("ver")
    if not isinstance(subject, str):
        return None
    # sub must stay an opaque uuid4 string: parseable UUIDs reject crafted ids
    # that could collide with other key spaces in the quota store.
    try:
        parsed_subject = uuid.UUID(subject)
    except ValueError:
        return None
    if parsed_subject.version != 4:
        return None
    if not isinstance(issued_at_epoch_seconds, int) or isinstance(issued_at_epoch_seconds, bool):
        return None
    if token_version != ANONYMOUS_IDENTITY_TOKEN_VERSION:
        return None

    age_seconds = current_epoch_seconds - issued_at_epoch_seconds
    within_freshness_window = age_seconds <= ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS
    issued_in_acceptable_past_or_present = age_seconds >= -CLOCK_SKEW_TOLERANCE_SECONDS
    if not (within_freshness_window and issued_in_acceptable_past_or_present):
        return None

    return AnonymousIdentityClaims(
        subject=subject,
        issued_at_epoch_seconds=issued_at_epoch_seconds,
        token_version=token_version,
    )


class RateLimitPort(Protocol):
    """Secondary abuse brake keyed by client IP (spec §9).

    Implemented in Issue 1B over Postgres ``ip_rate_limit``; the in-memory
    store below keeps this module standalone until that adapter lands.
    """

    async def check_rate_limit_allowed(
        self, ip_address: str, kind: str, limit: int, window_seconds: int
    ) -> bool: ...


class InMemoryRateLimitStore:
    """Fixed-window counter placeholder for the mint route before Issue 1B.

    Process-local by definition, so it bounds abuse per worker only; the
    Postgres store replaces it wholesale via set_rate_limit_port().
    """

    def __init__(self, *, now_provider: Callable[[], float] | None = None) -> None:
        self._counts_by_ip_kind_window: dict[tuple[str, str, int], int] = {}
        self._now_provider = now_provider or time.time
        self._last_observed_epoch: float | None = None

    async def check_rate_limit_allowed(
        self, ip_address: str, kind: str, limit: int, window_seconds: int
    ) -> bool:
        safe_window_seconds = max(window_seconds, 1)
        observed_epoch = self._now_provider()
        # A wall-clock rollback must never move a client back into an older
        # bucket, which could otherwise reset its allowance before the window
        # has elapsed. Clamp observations to the greatest value seen.
        if self._last_observed_epoch is not None:
            observed_epoch = max(observed_epoch, self._last_observed_epoch)
        self._last_observed_epoch = observed_epoch
        window_start_epoch = int(observed_epoch // safe_window_seconds) * safe_window_seconds
        counter_key = (ip_address, kind, window_start_epoch)
        current_count = self._counts_by_ip_kind_window.get(counter_key, 0)
        if current_count >= max(limit, 1):
            return False
        self._counts_by_ip_kind_window[counter_key] = current_count + 1
        return True


# Module-level seam instead of DI container wiring: the Postgres adapter
# (Issue 1B) installs itself once at startup, and tests override it freely.
_rate_limit_port: RateLimitPort = InMemoryRateLimitStore()


def get_rate_limit_port() -> RateLimitPort:
    return _rate_limit_port


def set_rate_limit_port(rate_limiter: RateLimitPort) -> None:
    global _rate_limit_port
    _rate_limit_port = rate_limiter


class AnonymousIdentityService:
    """Mints and verifies signed anonymous identity tokens.

    The signing secret arrives from Settings only — never constructed from
    literals — and an empty secret fails loudly rather than signing with a
    value an attacker could guess.
    """

    def __init__(
        self,
        signing_secret: str | bytes,
        *,
        now_provider: Callable[[], int] | None = None,
    ) -> None:
        if not signing_secret:
            raise ValueError("anonymous identity signing secret must not be empty")
        self._signing_secret = signing_secret
        self._now_provider = now_provider or (lambda: int(time.time()))

    def mint_token(self) -> str:
        return mint_anonymous_identity_token(
            self._signing_secret, now_epoch_seconds=self._now_provider()
        )

    def verify_token(self, token: str) -> AnonymousIdentityClaims | None:
        return verify_anonymous_identity_token(
            token, self._signing_secret, now_epoch_seconds=self._now_provider()
        )
