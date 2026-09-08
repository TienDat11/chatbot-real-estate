"""SSRF guard for outbound embedding/rerank HTTP calls.

Every outbound URL is validated BEFORE any request is made:
http/https schemes only, no credentials embedded in the URL, an exact
(case-insensitive) hostname allowlist match, and a DNS resolution pass that
rejects hosts resolving to loopback / private / link-local / multicast /
unspecified / reserved addresses. The DNS step is injectable so tests never
touch live DNS and production always uses socket resolution (fail closed).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = ("http", "https")


class OutboundUrlError(ValueError):
    """Raised when an outbound URL fails scheme/host/DNS validation."""


def _resolve_via_socket(host: str) -> list[str]:
    """Default resolver: all addresses socket would dial for this host."""
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def _is_forbidden_ip(ip_text: str) -> bool:
    address = ipaddress.ip_address(ip_text)
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    )


def validate_outbound_url(
    url: str,
    *,
    allowed_hosts: list[str] | tuple[str, ...],
    resolve_host: Callable[[str], list[str]] = _resolve_via_socket,
    allow_private: bool = False,
) -> str:
    """Return the validated URL or raise OutboundUrlError; never dials the host."""
    stripped = (url or "").strip()
    if not stripped:
        raise OutboundUrlError("outbound URL is empty")
    parts = urlsplit(stripped)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise OutboundUrlError(f"outbound URL scheme must be http/https, got {parts.scheme!r}")
    if parts.username or parts.password:
        raise OutboundUrlError("outbound URL must not embed credentials")
    host = (parts.hostname or "").lower()
    if not host:
        raise OutboundUrlError("outbound URL has no hostname")
    if not allow_private and host in ("localhost", "127.0.0.1", "::1"):
        raise OutboundUrlError(f"outbound host {host!r} is a localhost address (private forbidden)")
    allowed = {entry.strip().lower() for entry in allowed_hosts if entry and entry.strip()}
    if host not in allowed:
        raise OutboundUrlError(f"outbound host {host!r} is not on the configured allowlist")
    try:
        resolved = resolve_host(host)
    except OSError as exc:
        raise OutboundUrlError(f"outbound host {host!r} DNS resolution failed") from exc
    if not resolved:
        raise OutboundUrlError(f"outbound host {host!r} resolved to no addresses")
    if not allow_private:
        for ip_text in resolved:
            try:
                forbidden = _is_forbidden_ip(ip_text)
            except ValueError:
                forbidden = True  # unparseable address -> fail closed
            if forbidden:
                raise OutboundUrlError(
                    f"outbound host {host!r} resolves to a forbidden address range"
                )
    return stripped
