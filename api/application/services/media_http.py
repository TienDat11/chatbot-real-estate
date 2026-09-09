"""SSRF-safe outbound media fetch contract (spec §3.7 / ISSUE-13-BE6, bug B22).

No server-side outbound HTTP exists today; this module pins the contract any
FUTURE media fetcher must satisfy before touching the network (AC-20 BE half):

  1. scheme http/https only;
  2. host matched against an explicit origin allowlist BEFORE any request -
     exact ``scheme://host[:port]`` equality, no wildcard and no suffix match;
  3. literal hosts denied for localhost/loopback/private/link-local/reserved/
     multicast/metadata ranges, including IPv4-mapped and 6to4/Teredo IPv6
     disguises of those ranges;
  4. DNS resolved and EVERY returned A/AAAA record validated public - a single
     private record rejects the whole URL, and resolver failures fail closed;
  5. redirects are manual: zero hops by default; a caller may opt into
     following, but every hop target is re-validated through steps 2-4 with a
     fresh DNS resolution (Next's image optimizer does not re-check
     remotePatterns across redirects, so redirect targets are never trusted);
  6. response content-type allowlist plus size/timeout caps, and no credential
     headers are ever attached (userinfo-bearing URLs are rejected outright).

Fail-closed: any parse, allowlist, DNS or policy failure returns None instead
of guessing. Production origins stay in env (``image_cdn_project_map``) and
reach this module through ``media_config.allowed_media_origins(project_key)``;
keeping the module dependency-free (stdlib + httpx) lets rollback delete it
with zero serving-path impact.

WIRED CALLERS (stub label retired for these paths):
  - ``scripts/migrate_cdn_origins.py`` — CDN origin inventory / decision
    probes (ISSUE-3): every server-side HEAD passes through
    :func:`validate_media_fetch_url` + :func:`perform_media_head` with the
    project allowlist;
  - ``scripts/verify_cdn_media.py`` — operator verification gate probing the
    four canonical media URLs.
The hostile-URL matrix (tests/test_media_url_hostile.py) remains the
executable form of the contract.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("api.media_http")

__all__ = [
    "ALLOWED_CONTENT_TYPE_PREFIXES",
    "ALLOWED_SCHEMES",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_REDIRECT_HOPS_DEFAULT",
    "MediaHeadProbe",
    "normalize_allowed_origins",
    "perform_media_head",
    "validate_media_fetch_url",
]

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Well-known cloud metadata hostnames; their addresses sit inside the denied
# ranges below, but the names are denied too because resolution is
# environment-dependent (e.g. /etc/hosts overrides).
METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "instance-data.ec2.internal",
        "100.100.100.200",
    }
)


def _canonical_hostname(hostname: str) -> str | None:
    """Return a lowercase ASCII hostname, rejecting invalid IDN forms."""
    try:
        value = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeEncodeError):
        return None
    return value or None


def _canonical_port(parsed) -> int | None:
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return 443 if parsed.scheme.lower() == "https" else 80
    return port


def _canonical_origin(parsed) -> str | None:
    host = _canonical_hostname(parsed.hostname or "")
    if host is None or parsed.username is not None or parsed.password is not None:
        return None
    port = _canonical_port(parsed)
    if port is None:
        return None
    host_part = f"[{host}]" if ":" in host else host
    default_port = (parsed.scheme.lower() == "https" and port == 443) or (
        parsed.scheme.lower() == "http" and port == 80
    )
    return f"{parsed.scheme.lower()}://{host_part}{'' if default_port else f':{port}'}"

_LOCAL_HOSTNAMES = frozenset({"localhost"})
_LOCAL_SUFFIXES = (".localhost",)

_DENIED_V4_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",        # this-network (incl. 0.0.0.0 unspecified)
        "10.0.0.0/8",       # RFC1918 private
        "100.64.0.0/10",    # CGNAT shared space (incl. Alibaba 100.100.100.200)
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local (incl. 169.254.169.254 EC2/metadata)
        "172.16.0.0/12",    # RFC1918 private
        "192.0.0.0/24",     # IETF protocol assignments
        "192.0.2.0/24",     # TEST-NET-1 (documentation)
        "192.168.0.0/16",   # RFC1918 private
        "198.18.0.0/15",    # benchmarking
        "198.51.100.0/24",  # TEST-NET-2 (documentation)
        "203.0.113.0/24",   # TEST-NET-3 (documentation)
        "224.0.0.0/4",      # IPv4 multicast
        "240.0.0.0/4",      # reserved (incl. 255.255.255.255 broadcast)
    )
)

_DENIED_V6_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "::/128",         # unspecified
        "::1/128",        # loopback
        "64:ff9b::/96",   # well-known NAT64 (embeds IPv4)
        "100::/64",       # discard-only
        "2001::/32",      # Teredo (embeds IPv4)
        "2001:db8::/32",  # documentation
        "2002::/16",      # 6to4 (embeds IPv4)
        "fc00::/7",       # unique-local private
        "fe80::/10",      # link-local
        "ff00::/8",       # IPv6 multicast
    )
)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_REDIRECT_HOPS_DEFAULT = 0  # spec §3.7 step 5: following redirects is opt-in

ALLOWED_CONTENT_TYPE_PREFIXES = ("image/", "video/")

_USER_AGENT = "ragre-media-contract/1.0"

# Injected so tests can pin DNS-dependent behavior without touching the network;
# production callers get real resolution via loop.getaddrinfo (A + AAAA).
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def _default_resolver(host: str, port: int) -> list[str]:
    """Resolve one host to every A/AAAA record via stdlib getaddrinfo."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ordered: dict[str, None] = {}
    for info in infos:
        ordered.setdefault(info[4][0], None)
    return list(ordered)


def _address_denial_reason(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return why one address is non-public, or None when it is safe to contact.

    IPv4-mapped IPv6 (::ffff:a.b.c.d) is unwrapped before checking: the embedded
    v4 form must obey v4 rules, otherwise ::ffff:127.0.0.1 slips past the v4
    denylist as an "IPv6" address.
    """
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return _address_denial_reason(mapped)
    networks = _DENIED_V6_NETWORKS if address.version == 6 else _DENIED_V4_NETWORKS
    for network in networks:
        if address in network:
            return f"{network} denies {address}"
    return None


def _hostname_denial_reason(hostname: str) -> str | None:
    """Static pre-DNS host denial: metadata names, localhost forms, literal IPs."""
    bare = hostname.lower().rstrip(".")
    if bare in METADATA_HOSTNAMES:
        return f"cloud metadata hostname {bare}"
    if bare in _LOCAL_HOSTNAMES or bare.endswith(_LOCAL_SUFFIXES):
        return f"local hostname {bare}"
    try:
        literal = ipaddress.ip_address(bare)
    except ValueError:
        return None
    return _address_denial_reason(literal)


def _normalize_origin(value: object) -> str | None:
    """Canonicalize one allowlist entry to ``scheme://host[:port]`` or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
        return None
    return _canonical_origin(parsed)


def normalize_allowed_origins(allowed_origins: Iterable[object]) -> frozenset[str]:
    """Canonicalize caller-supplied origins; unusable entries are dropped.

    Dropping (rather than raising) keeps the gate fail-closed: a malformed env
    entry shrinks the allowlist instead of widening it.
    """
    return frozenset(
        origin
        for origin in (_normalize_origin(value) for value in allowed_origins or ())
        if origin is not None
    )


async def validate_media_fetch_url(
    url: object,
    allowed_origins: Iterable[object],
    *,
    resolve: Resolver = _default_resolver,
) -> str | None:
    """Apply spec §3.7 steps 1-4 to one candidate media URL; None = rejected.

    Order matters: the exact allowlist match (step 2) runs before any DNS work,
    so attacker-controlled names never trigger resolution, and step 4 then
    demands a public verdict on EVERY A/AAAA record (DNS rebinding via one
    private record is rejected wholesale). Returns the stripped URL on success;
    callers must still connect to a freshly resolved validated address per
    attempt (see :func:`perform_media_head`).
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
        return None  # step 1
    origin = _canonical_origin(parsed)
    if origin is None or origin not in normalize_allowed_origins(allowed_origins):
        return None  # step 2: exact match only - wildcard/suffix never qualifies
    hostname = _canonical_hostname(parsed.hostname)
    if hostname is None:
        return None
    reason = _hostname_denial_reason(hostname)
    if reason is not None:
        logger.debug("media fetch url rejected (%s)", reason)
        return None  # step 3
    effective_port = _canonical_port(parsed)
    if effective_port is None:
        return None
    try:
        records = await resolve(hostname, effective_port)
    except OSError as exc:
        logger.debug("media fetch url rejected (resolver error: %s)", exc)
        return None  # step 4: fail closed on resolver failure
    if not records:
        return None
    for record in records:
        try:
            address = ipaddress.ip_address(record)
        except ValueError:
            return None
        if _address_denial_reason(address) is not None:
            logger.debug("media fetch url rejected (non-public record %s)", record)
            return None
    return text


@dataclass(frozen=True)
class MediaHeadProbe:
    """Verdict snapshot of one completed, policy-passing media response."""

    url: str
    status_code: int
    content_type: str
    content_length: int | None


async def perform_media_head(
    url: str,
    allowed_origins: Iterable[object],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    method: str = "HEAD",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_redirect_hops: int = MAX_REDIRECT_HOPS_DEFAULT,
    resolve: Resolver = _default_resolver,
    user_agent: str | None = None,
) -> MediaHeadProbe | None:
    """Probe one media URL under the full §3.7 contract; None = rejected.

    Some CDN edges (Cloudflare ``r2.dev``) refuse unconventional tool user
    agents before policy even matters; an operator-facing caller may pass a
    conventional ``user_agent`` string. Only ever this ONE header value plus
    Accept is attached — never credentials.

    WIRED (no longer a stub-only helper; ISSUE-3): inventory/verification
    callers are scripts/migrate_cdn_origins.py and scripts/verify_cdn_media.py.
    Redirects are
    manual: ``max_redirect_hops=0`` (default) never follows a 3xx, and every
    followed hop re-runs :func:`validate_media_fetch_url` with a fresh DNS
    resolution (the spec's re-resolve-per-attempt clause). Known residual risk,
    documented for the future fetcher owner: the OS resolver runs again inside
    httpx at connect time, so a rebinding resolver can race the validated
    address; closing that gap requires binding the socket to the validated IP
    via a custom transport.

    No credential headers are ever attached: requests carry only Accept and
    User-Agent, and userinfo-bearing URLs were already rejected upstream.
    """
    verb = method.upper()
    if verb not in {"HEAD", "GET"}:
        raise ValueError("perform_media_head supports HEAD/GET only")
    headers = {
        "Accept": ", ".join(prefix + "*" for prefix in ALLOWED_CONTENT_TYPE_PREFIXES),
        "User-Agent": user_agent or _USER_AGENT,
    }
    current = url
    hops_left = max_redirect_hops
    async with httpx.AsyncClient(
        follow_redirects=False,  # step 5: redirects handled manually, never implicitly
        timeout=httpx.Timeout(timeout_seconds),  # step 6: hard deadline
        transport=transport,
    ) as client:
        while True:
            validated = await validate_media_fetch_url(
                current, allowed_origins, resolve=resolve
            )
            if validated is None:
                return None
            request = client.build_request(verb, validated, headers=headers)
            response = await client.send(request, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if hops_left <= 0 or not location:
                        return None  # maximumRedirects=0 semantics by default
                    hops_left -= 1
                    current = str(httpx.URL(validated).join(location))
                    continue  # next hop re-validated through steps 2-4
                content_type = response.headers.get("content-type", "")
                content_type = content_type.split(";")[0].strip().lower()
                if not content_type.startswith(ALLOWED_CONTENT_TYPE_PREFIXES):
                    return None  # step 6: content-type allowlist
                raw_length = response.headers.get("content-length")
                declared = int(raw_length) if raw_length and raw_length.isdigit() else None
                if declared is not None and declared > max_response_bytes:
                    return None  # step 6: declared size cap
                if verb == "GET":
                    consumed = 0
                    async for chunk in response.aiter_bytes():
                        consumed += len(chunk)
                        if consumed > max_response_bytes:
                            return None  # body exceeded the cap midstream
                return MediaHeadProbe(
                    url=validated,
                    status_code=response.status_code,
                    content_type=content_type,
                    content_length=declared,
                )
            finally:
                await response.aclose()
