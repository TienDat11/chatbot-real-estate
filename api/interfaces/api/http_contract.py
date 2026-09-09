"""Shared staff-HTTP contract primitives (G3-r6 spec §10.2).

One place for the cross-cutting headers every staff route must carry:
``X-Correlation-ID`` (echoed when the client sent a valid one, otherwise
server-generated) and the private no-store cache discipline on PII/transcript
GETs. Keeping the logic here — not in each router — guarantees the shapes can
never drift between the CRM, sales-notification, and training surfaces.
"""

from __future__ import annotations

import re
import uuid

from fastapi import Request

# Bounded to a URL/header-safe token; anything longer or with unexpected
# characters is treated as absent and a fresh server id is minted, so a client
# can never smuggle an unbounded string into logs or downstream audit records.
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store, private",
    "Vary": "Authorization",
}


def resolve_correlation_id(request: Request) -> str:
    """Return the request's X-Correlation-ID when valid, else a server id."""
    candidate = request.headers.get("X-Correlation-ID") or ""
    if _CORRELATION_ID_PATTERN.fullmatch(candidate):
        return candidate
    return f"corr-{uuid.uuid4().hex}"


def staff_no_store_headers(correlation_id: str) -> dict[str, str]:
    """Headers for a PII/transcript-bearing staff GET response."""
    return {"X-Correlation-ID": correlation_id, **NO_STORE_HEADERS}


def error_headers_with_correlation(correlation_id: str) -> dict[str, str]:
    """Headers for an HTTPException raised on a staff contract route.

    FastAPI exception handlers build a fresh response, so the ``Response``
    parameter never reaches error bodies; every staff error answer must
    still carry the correlation id for log/audit joins.
    """
    return {"X-Correlation-ID": correlation_id}


__all__ = [
    "NO_STORE_HEADERS",
    "error_headers_with_correlation",
    "resolve_correlation_id",
    "staff_no_store_headers",
]
