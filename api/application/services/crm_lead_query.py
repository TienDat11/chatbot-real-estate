"""Server-paginated CRM leads page query (plan BE-LEADS-QUERY / spec FR-31).

Owns the three pieces of the leads-page contract that must never leak into
the route or the adapter: role scoping (a sales principal is pinned to its
own ``assigned_sales_id`` server-side — no client parameter can widen it),
opaque cursor round-tripping (base64 ``"<created_at iso>|<id>"``, strictly
validated so malformed cursors surface as ``ValueError`` -> 422), and the
business-timezone resolution for the reengage window (env
``CRM_REENGAGE_FILTER_TIMEZONE``, default ``Asia/Ho_Chi_Minh``, UTC fallback
with a warning on any invalid value).
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger("api.services.crm_lead_query")

FILTER_TIMEZONE_ENV = "CRM_REENGAGE_FILTER_TIMEZONE"
DEFAULT_FILTER_TIMEZONE = "Asia/Ho_Chi_Minh"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class _LeadsSearchRepo(Protocol):
    async def search_crm_leads(
        self,
        *,
        assigned_sales_id: int | None,
        project_key: str | None,
        status: str | None,
        reengage_from: date | None,
        reengage_to: date | None,
        tz_name: str,
        cursor_created_at: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list: ...


class CrmLeadsPageQuery:
    """Validated, owner-agnostic page request; scoping is applied from the
    verified principal, never from client data."""

    __slots__ = (
        "project_key",
        "status",
        "reengage_from",
        "reengage_to",
        "cursor",
        "limit",
    )

    def __init__(
        self,
        *,
        project_key: str | None = None,
        status: str | None = None,
        reengage_from: date | None = None,
        reengage_to: date | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.project_key = project_key
        self.status = status
        self.reengage_from = reengage_from
        self.reengage_to = reengage_to
        self.cursor = cursor
        self.limit = min(max(int(limit), 1), MAX_LIMIT)


@dataclass(frozen=True)
class CrmLeadsPage:
    """One resolved page of leads plus the keyset continuation token."""

    rows: list
    has_more: bool
    next_cursor: str | None


def resolve_filter_timezone_name() -> str:
    """Resolve the IANA tz name used to translate reengage calendar days.

    Read once per call (not cached) so ops can change the env without a
    restart. An unset env means the Vietnam-market default; a set-but-invalid
    value means UTC plus a warning — never a crash.
    """
    raw = os.environ.get(FILTER_TIMEZONE_ENV, DEFAULT_FILTER_TIMEZONE)
    try:
        ZoneInfo(raw)
    except Exception:  # noqa: BLE001 — ZoneInfo raises several error types
        logger.warning(
            "invalid %s=%r; reengage window falls back to UTC",
            FILTER_TIMEZONE_ENV,
            raw,
        )
        return "UTC"
    return raw


def encode_leads_cursor(created_at: datetime, lead_id: int) -> str:
    payload = f"{created_at.isoformat()}|{lead_id}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_leads_cursor(cursor: str) -> tuple[datetime, int]:
    """Decode ``base64("<created_at iso>|<id>")``; raise ValueError on ANY
    defect (bad base64, missing separator, non-ISO timestamp, non-int id) so
    the route maps it to 422 instead of leaking a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        created_part, separator, id_part = raw.rpartition("|")
        if not separator:
            raise ValueError("cursor separator missing")
        iso = created_part[:-1] + "+00:00" if created_part.endswith("Z") else created_part
        created_at = datetime.fromisoformat(iso)
        lead_id = int(id_part)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc
    return created_at, lead_id


async def query_crm_leads_page(
    repo: _LeadsSearchRepo,
    *,
    principal,
    query: CrmLeadsPageQuery,
) -> CrmLeadsPage:
    """Resolve one page: scope -> decode cursor -> repo (limit+1) -> slice.

    ``principal.role == "sales"`` pins ``assigned_sales_id`` to the verified
    ``principal.sales_id``; any other role (admin) is unrestricted. The
    client-visible query fields are passed through untouched.
    """
    assigned_sales_id = principal.sales_id if principal.role == "sales" else None
    if principal.role == "sales" and principal.sales_id is None:
        # Fail closed: require_sales_or_admin normally 403s a sales token
        # without a PG mapping before this service runs, but never let a
        # None scope degenerate into "see everything".
        return CrmLeadsPage(rows=[], has_more=False, next_cursor=None)
    cursor_created_at: datetime | None = None
    cursor_id: int | None = None
    if query.cursor:
        cursor_created_at, cursor_id = decode_leads_cursor(query.cursor)
    rows = await repo.search_crm_leads(
        assigned_sales_id=assigned_sales_id,
        project_key=query.project_key,
        status=query.status,
        reengage_from=query.reengage_from,
        reengage_to=query.reengage_to,
        tz_name=resolve_filter_timezone_name(),
        cursor_created_at=cursor_created_at,
        cursor_id=cursor_id,
        limit=query.limit + 1,
    )
    has_more = len(rows) > query.limit
    page = rows[: query.limit]
    # Cursor of the LAST returned row (keyset continuation); None on the
    # final page so the client stops asking.
    next_cursor = encode_leads_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return CrmLeadsPage(rows=page, has_more=has_more, next_cursor=next_cursor)
