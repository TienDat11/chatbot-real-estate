"""Project identity from the project_config registry (stories 8.2 + 10.2).

Single registry source the answer path reads for per-project identity fields
(ten_thuong_mai, ten_phap_ly, vi_tri, hotline) and the brand token used by the
deterministic intent classifier. Story 10.2: prompts, greeting, and conv
directives are parameterized with {project_*} placeholders and rendered at
runtime against this registry, so a second active project stops inheriting
Camellia's name and location.

Async answer path (B2/M1): the async entry points (query pipeline facades,
hello handlers) read the registry ONCE per request through the
ProjectRegistryPort and bind the record into a per-request snapshot
(contextvar). The legacy synchronous helpers below consult that snapshot
first, so inside a bound request they never touch the DB at all; a degraded
read (record None, still bound) serves the static defaults without a sync
retry.

Best-effort by contract outside the bound path: every read is synchronous
with a short connect timeout and degrades to the Camellia defaults below, so
a dead DB never crashes sync callers (scripts, ingest/eval CLIs, unit-test
seams) and pre-project callers behave exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from api.application.ports.project_registry import ProjectRegistryRecord
from api.infrastructure.config.config import settings

logger = logging.getLogger("api.project_config")

# Default project for legacy callers that predate project_key (story 10.1
# back-compat) and for the degraded fallback when the registry read fails.
DEFAULT_PROJECT_KEY = "camellia"

# Camellia identity snapshot mirroring db/seed/project_config.sql. Kept here so
# the answer path has ONE source for the legacy defaults; the registry row is
# authoritative whenever it is reachable.
_CAMELLIA_IDENTITY: dict[str, str] = {
    "ten_thuong_mai": "The Camellia Son Tra - Da Nang",
    "ten_phap_ly": "Trung tâm Thương mại, văn phòng cho thuê và nhà ở cao tầng",
    "vi_tri": "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng",
    "hotline": "0345 747 138",
}

# Placeholders a prompt/greeting may carry; resolved from the registry at render
# time. {project} is the project_key itself (short stable namespace).
_PLACEHOLDERS = ("ten_thuong_mai", "ten_phap_ly", "vi_tri", "hotline", "project")


# --- per-request registry snapshot (B2: one async read, zero sync reads) -----

# Sentinel distinguishing "no async request bound a snapshot yet" (legacy sync
# callers keep the psycopg2 fallback) from "the async path already read the
# registry and got nothing" (degraded: serve defaults, never retry sync).
_SNAPSHOT_UNBOUND = object()

_REQUEST_PROJECT_SNAPSHOT: ContextVar = ContextVar(
    "ragre_request_project_snapshot", default=_SNAPSHOT_UNBOUND
)


def request_project_snapshot() -> "ProjectRegistryRecord | None":
    """Return the per-request registry record; None when unbound or degraded."""
    value = _REQUEST_PROJECT_SNAPSHOT.get()
    return None if value is _SNAPSHOT_UNBOUND else value


def request_project_snapshot_bound() -> bool:
    """True when an async entry point already attempted the registry read."""
    return _REQUEST_PROJECT_SNAPSHOT.get() is not _SNAPSHOT_UNBOUND


async def load_project_registry_record(
    project_key: "str | None",
) -> "ProjectRegistryRecord | None":
    """Read the registry row for one project through the async port.

    Returns None immediately for an empty key (no DB round trip); the adapter
    itself degrades to None on any failure.
    """
    if not project_key:
        return None
    from api.infrastructure.dependencies import get_project_registry  # noqa: PLC0415

    return await get_project_registry().fetch_project(project_key)


@contextmanager
def bound_request_project(
    record: "ProjectRegistryRecord | None",
) -> "Iterator[ProjectRegistryRecord | None]":
    """Bind the per-request registry record for the legacy sync helpers.

    Binding None is meaningful: it tells the helpers the async read already
    ran and failed, so they serve static defaults instead of retrying the DB
    synchronously (B2: no sync psycopg2 inside async handlers, ever).
    """
    token = _REQUEST_PROJECT_SNAPSHOT.set(record)
    try:
        yield record
    finally:
        _REQUEST_PROJECT_SNAPSHOT.reset(token)


def identity_from_record(record: ProjectRegistryRecord) -> dict[str, str]:
    """Identity fields of a record with per-field Camellia fallback (NULL col)."""
    return {
        "ten_thuong_mai": record.ten_thuong_mai or _CAMELLIA_IDENTITY["ten_thuong_mai"],
        "ten_phap_ly": record.ten_phap_ly or _CAMELLIA_IDENTITY["ten_phap_ly"],
        "vi_tri": record.vi_tri or _CAMELLIA_IDENTITY["vi_tri"],
        "hotline": record.hotline or _CAMELLIA_IDENTITY["hotline"],
    }


def default_identity() -> dict[str, str]:
    """Copy of the static Camellia identity (the degraded default)."""
    return dict(_CAMELLIA_IDENTITY)


def fetch_project_identity(project_key: str | None = None) -> dict[str, str]:
    """Return identity fields for a project; Camellia defaults on any failure.

    ``project_key`` None resolves to DEFAULT_PROJECT_KEY so legacy callers keep
    the Camellia identity. Inside a request-bound snapshot the registry record
    is used directly (or the defaults when the async read degraded) and the DB
    is never touched. Outside it, the registry row read is a short synchronous
    psycopg2 best-effort kept for sync callers (scripts, unit-test seams).
    """
    key = project_key or DEFAULT_PROJECT_KEY
    if request_project_snapshot_bound():
        record = request_project_snapshot()
        if record is not None and record.project_key == key:
            return identity_from_record(record)
        return default_identity()
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; using default project identity")
        return default_identity()
    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ten_thuong_mai, ten_phap_ly, vi_tri, hotline "
                    "FROM project_config WHERE project_key = %s",
                    (key,),
                )
                row = cur.fetchone()
        if row is None:
            logger.warning("project_config row missing for %s; using defaults", key)
            return default_identity()
        return {
            "ten_thuong_mai": row[0] or _CAMELLIA_IDENTITY["ten_thuong_mai"],
            "ten_phap_ly": row[1] or _CAMELLIA_IDENTITY["ten_phap_ly"],
            "vi_tri": row[2] or _CAMELLIA_IDENTITY["vi_tri"],
            "hotline": row[3] or _CAMELLIA_IDENTITY["hotline"],
        }
    except Exception as exc:  # noqa: BLE001 — registry read is best-effort
        logger.warning("project_config identity read failed (%s); using defaults", exc)
        return default_identity()


def render_template(text: str, project_key: str | None = None) -> str:
    """Substitute {placeholder} tokens with the project's registry values.

    Unknown placeholder tokens are left untouched so an unrendered literal can
    never silently become part of a prompt (a missing token in the source file
    fails loud in review). None/empty text returns unchanged.
    """
    if not text:
        return text
    identity = fetch_project_identity(project_key)
    values: dict[str, str] = {
        "project": project_key or DEFAULT_PROJECT_KEY,
        **identity,
    }
    out = text
    for name in _PLACEHOLDERS:
        out = out.replace("{" + name + "}", values.get(name, ""))
    return out


def brand_token(project_key: str | None = None) -> str:
    """First significant word of ten_thuong_mai (lowercased, no leading 'The').

    Feeds the deterministic company-intent keyword ("X là của ai", story 10.2):
    'The Camellia Son Tra - Da Nang' -> 'camellia', 'The Soleil ...' -> 'soleil'.
    A name that yields no token falls back to the project_key itself.
    """
    identity = fetch_project_identity(project_key)
    name = (identity.get("ten_thuong_mai") or "").strip()
    lowered = name.lower()
    if lowered.startswith("the "):
        lowered = lowered[4:]
    match = re.search(r"[a-z0-9]+", lowered)
    token = match.group(0) if match else ""
    return token or (project_key or DEFAULT_PROJECT_KEY)


def fetch_projects() -> list[dict[str, Any]]:
    """Return the active project catalogue for GET /api/projects (story 10.3).

    Mirrors the best-effort contract of fetch_project_identity/project_geo_center
    (short sync psycopg2 read, degrade instead of crash): a dead DB yields an
    empty list so the endpoint returns ``projects: []`` (200) and the FE picker
    falls back to its static catalogue. ``location`` falls back to vi_tri for
    rows seeded before the location column existed. Ordering is HOT-first then
    name so Camellia always leads the picker.
    """
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; projects catalogue empty")
        return []
    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT project_key, ten_thuong_mai, "
                    "COALESCE(location, vi_tri) AS location, "
                    "geo_center_lat, geo_center_lng, is_hot "
                    "FROM project_config WHERE status = 'active' "
                    "ORDER BY is_hot DESC, ten_thuong_mai"
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — catalogue read is best-effort
        logger.warning("project_config projects read failed (%s); returning []", exc)
        return []
    projects = [
        {
            "project_key": row[0],
            "name": row[1],
            "location": row[2],
            "lat": float(row[3]) if row[3] is not None else None,
            "lng": float(row[4]) if row[4] is not None else None,
            "is_hot": bool(row[5]),
        }
        for row in rows
    ]
    # Deterministic contract order regardless of DB collation: HOT first, then by
    # (case-insensitive) name.
    projects.sort(key=lambda p: (not p["is_hot"], (p["name"] or "").lower()))
    return projects


def project_catalogue_from_records(
    records: list[ProjectRegistryRecord],
) -> list[dict[str, Any]]:
    """Shape registry records into the GET /api/projects contract dicts.

    Single mapping for every catalogue consumer so the contract (keys, float
    coords, HOT-first ordering) cannot drift between call sites.
    """
    projects = [
        {
            "project_key": r.project_key,
            "name": r.ten_thuong_mai,
            "location": r.location,
            "lat": r.geo_center_lat,
            "lng": r.geo_center_lng,
            "is_hot": r.is_hot,
        }
        for r in records
    ]
    projects.sort(key=lambda p: (not p["is_hot"], (p["name"] or "").lower()))
    return projects


async def load_project_catalogue() -> list[dict[str, Any]]:
    """Async catalogue read for the 422 PROJECT_SCOPE body (M12).

    ``fetch_projects`` is the legacy sync seam (psycopg2, unit-test patched),
    so the async caller offloads it to a worker thread: the event loop never
    blocks on the sync driver while the monkeypatch seam stays intact.
    """
    return await asyncio.to_thread(fetch_projects)


__all__ = [
    "DEFAULT_PROJECT_KEY",
    "ProjectRegistryRecord",
    "fetch_project_identity",
    "identity_from_record",
    "default_identity",
    "render_template",
    "brand_token",
    "fetch_projects",
    "project_catalogue_from_records",
    "load_project_catalogue",
    "load_project_registry_record",
    "bound_request_project",
    "request_project_snapshot",
    "request_project_snapshot_bound",
]
