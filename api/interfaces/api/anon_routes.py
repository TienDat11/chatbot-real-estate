"""Anonymous identity minting API (secure wave spec §5.6).

Thin route only: token construction lives in the application service and
the IP brake sits behind RateLimitPort, so Issue 1B swaps in the Postgres
store without touching this file. The router is exposed for Issue 2 to
include in main.py — registration deliberately does NOT happen here.
"""

from __future__ import annotations

import hashlib
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from api.application.services.anon_identity import (
    ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS,
    RATE_LIMIT_KIND_MINT,
    AnonymousIdentityService,
    RateLimitPort,
    get_rate_limit_port,
)
from api.infrastructure.config.config import get_settings

router = APIRouter(prefix="/api/anon", tags=["anon"])


class AnonTokenResponse(BaseModel):
    anon_token: str
    expires_hint_hours: int


def get_trusted_proxy_ips() -> set[str]:
    """Configured reverse-proxy peers allowed to append X-Forwarded-For.

    Also used as a FastAPI dependency for the mint route (tests override it
    there); main.py's plain-function call path reads it via settings.
    """
    return set(get_settings().trusted_proxy_ips)


def get_client_ip_address(
    request: Request, *, trusted_proxy_ips: set[str] | None = None
) -> str | None:
    """Canonical client IP for the per-IP brakes (spec §9).

    X-Forwarded-For is honored ONLY when the immediate TCP peer
    (request.client.host) is a configured trusted proxy; otherwise the header
    is attacker-controlled and a direct client could burn another address's
    allowance by forging it. When the peer is trusted, the header is walked
    from RIGHT to LEFT, skipping any hop that is itself a trusted proxy; the
    first non-trusted hop from the right is the nearest client we can verify —
    a chain of trusted proxies cannot be made to vouch for a hop we never saw
    connect. A direct connection never reads XFF: the peer address is the
    canonical IP.
    """
    peer_host = request.client.host if request.client else ""
    trusted = trusted_proxy_ips if trusted_proxy_ips is not None else get_trusted_proxy_ips()
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and peer_host in trusted:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        for hop in reversed(hops):
            if hop not in trusted:
                return hop
        if hops:
            return hops[0]
    return peer_host or None


def _normalized_client_ip_or_none(raw_ip_address: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address(raw_ip_address))
    except (TypeError, ValueError):
        return None


def _synthetic_ip_for_peer(peer: str | None) -> str | None:
    """Map a non-IP peer to a deterministic address in the TEST-NET-like range."""
    if not peer or not peer.strip():
        return None
    digest = hashlib.sha256(peer.encode("utf-8")).digest()
    # 17 bits identify peers within 198.18.0.0/15 (RFC 2544 benchmark range).
    offset = int.from_bytes(digest[:3], "big") & 0x1FFFF
    return str(ipaddress.ip_address(int(ipaddress.ip_address("198.18.0.0")) + offset))


def _rate_limit_client_ip(request: Request, *, trusted_proxy_ips: set[str]) -> str | None:
    """Return an INET-safe rate-limit key, failing closed without a peer."""
    raw_identity = get_client_ip_address(request, trusted_proxy_ips=trusted_proxy_ips)
    normalized = _normalized_client_ip_or_none(raw_identity)
    return normalized or _synthetic_ip_for_peer(raw_identity)


def get_anonymous_identity_service() -> AnonymousIdentityService:
    return AnonymousIdentityService(get_settings().anon_identity_secret)


def get_mint_request_limit() -> int:
    return get_settings().ip_rate_limit_mint_max_requests


def get_mint_window_seconds() -> int:
    return get_settings().ip_rate_limit_window_seconds


@router.get("/token", response_model=AnonTokenResponse)
async def mint_anonymous_identity_token_endpoint(
    request: Request,
    identity_service: AnonymousIdentityService = Depends(get_anonymous_identity_service),  # noqa: B008
    rate_limiter: RateLimitPort = Depends(get_rate_limit_port),  # noqa: B008
    mint_request_limit: int = Depends(get_mint_request_limit),
    mint_window_seconds: int = Depends(get_mint_window_seconds),
    trusted_proxy_ips: set[str] = Depends(get_trusted_proxy_ips),  # noqa: B008
) -> AnonTokenResponse:
    client_ip_address = _rate_limit_client_ip(request, trusted_proxy_ips=trusted_proxy_ips)
    mint_allowed = client_ip_address is not None and await rate_limiter.check_rate_limit_allowed(
        client_ip_address,
        RATE_LIMIT_KIND_MINT,
        mint_request_limit,
        mint_window_seconds,
    )
    if not mint_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many anonymous identities requested from this address",
        )
    return AnonTokenResponse(
        anon_token=identity_service.mint_token(),
        expires_hint_hours=ANONYMOUS_IDENTITY_FRESHNESS_WINDOW_SECONDS // 3600,
    )
