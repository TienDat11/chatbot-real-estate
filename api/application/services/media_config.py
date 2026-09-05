"""Per-project video registry for the greeting widget (story 8.2 / ISSUE-01).

The registry source moved from a frozen module constant to the
``project_config.media`` column so each project carries its own R2 video bundle
(Soleil vs Camellia differ). Each DB entry stores R2 *object keys* plus display
metadata; the public URL is resolved against ``settings.r2_public_base`` at call
time so one row works across environments (dev vs prod R2 hosts), mirroring the
key-based convention of ``ingest/upload_videos_r2.py``.

WHY DB now and not a literal: the multi-project registry (project_config) is the
single place an operator adds a project; hardcoding the bundle here again would
fork the truth the moment a second project ships videos. There is deliberately
NO static fallback bundle: Camellia clips under a Soleil greeting would be
cross-project media, so a missing/empty registry row resolves to an empty list
(media is best-effort; the greeting renders fine without videos).

Async answer path (B2/M1): inside a request-bound registry snapshot the media
entries come from the record the async handler already loaded — the DB is never
touched here. The sync psycopg2 read remains only for unbound legacy sync
callers (direct unit-test seams / scripts).

Callers that predate project_key (api/interfaces/api/hello.py) call with no
arguments; the Camellia default keeps them working until ISSUE-05 scopes the
call sites by project.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import unquote, urlsplit

from api.application.services.project_config import (
    request_project_snapshot,
    request_project_snapshot_bound,
)
from api.infrastructure.config.config import settings

logger = logging.getLogger("api.media_config")

# Public R2 base resolved at import: settings.r2_public_base honors a custom
# domain when configured and otherwise falls back to the account-derived
# pub-<account_id>.r2.dev host, so the fallback never hardcodes an environment.
_public_base = settings.r2_public_base

# Display contract the frontend consumes; extra DB fields are dropped here.


def _configured_origin(value: object) -> str | None:
    """Normalize one explicit public origin without exposing URL credentials."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    # Reject any userinfo, including an explicitly empty username. An empty
    # username is still credential-bearing syntax and must not cross the media
    # trust boundary; checking ``is not None`` also covers ``https://@host``.
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def _project_media_policy(project_key: str | None) -> tuple[set[str], tuple[str, ...]]:
    """Return explicit origins and the stored object-key prefixes for a project.

    The map accepts either the legacy ``{"project": "https://host"}`` shape or
    the explicit shape ``{"project": {"origins": [...], "path_prefixes": [...]}}``.
    Prefixes are deliberately based on the existing storage conventions: Camellia
    legacy rows use ``media/video/`` and ``images/matbang/``; the uploader uses
    ``images/<project>/`` for newer corpora.
    """
    if not project_key:
        return set(), ()
    try:
        mapping = json.loads(settings.image_cdn_project_map or "{}")
    except (TypeError, ValueError):
        mapping = {}
    has_mapping = isinstance(mapping, dict) and bool(mapping)
    raw = mapping.get(project_key) if has_mapping else None
    if raw is None:
        # Development/test fixtures commonly inject only R2_ACCOUNT_ID (or a
        # public URL) and intentionally omit the per-project map. Derive the
        # provider-owned R2 origin in those environments, but keep production
        # fail-closed: an absent project map never authorizes media there.
        if not has_mapping and settings.app_env.strip().lower() in {"dev", "development", "test"}:
            origin = _configured_origin(settings.r2_public_base)
            if origin:
                return {origin}, ()
        return set(), ()
    if isinstance(raw, dict):
        values = raw.get("origins", raw.get("origin", []))
        prefixes = raw.get("path_prefixes", [])
    else:
        # Legacy string entries are shorthand, not a second source of truth.
        # Resolve strings through Settings so an explicit current R2_PUBLIC_URL
        # replaces stale account-derived hosts. Explicit lists remain an
        # allowlist for tests/custom deployments and are not stringified.
        values = (
            settings.image_cdn_base(project_key)
            if isinstance(raw, str)
            else raw
        )
        prefixes = []
    values = values if isinstance(values, list) else [values]
    origins = {origin for value in values if (origin := _configured_origin(value))}
    if not prefixes:
        prefixes = list(settings.image_cdn_path_prefixes(project_key))
    if not prefixes:
        # Per-project defaults only: the bare "images/banggia/" namespace is
        # Camellia's legacy layout and MUST NOT default into other projects'
        # prefixes — on a shared bucket host it would bless another project's
        # price-board URLs into this project's media policy. Non-Camellia
        # projects resolve strictly to their own images/<project_key>/ tree;
        # a deployment wanting the legacy namespace states it explicitly.
        prefixes = (
            [
                f"images/{project_key}/",
                "images/matbang/",
                "images/banggia/",
                "media/video/",
            ]
            if project_key == "camellia"
            else [f"images/{project_key}/"]
        )
    valid_prefixes = tuple(
        str(prefix).strip().lstrip("/")
        for prefix in prefixes
        if isinstance(prefix, str) and str(prefix).strip()
    )
    return origins, valid_prefixes


def allowed_media_origins(project_key: str | None) -> set[str]:
    """Return only explicitly mapped origins; the global R2 host is never implicit."""
    return _project_media_policy(project_key)[0]


def valid_media_url(
    value: object, project_key: str | None, media_kind: str | None = None
) -> str | None:
    """Validate explicit project origin and, for media, its object-key namespace."""
    if not isinstance(value, str) or not project_key:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    origin = _configured_origin(value)
    if not origin or origin not in allowed_media_origins(project_key):
        return None
    if media_kind:
        path = unquote(parsed.path).lstrip("/")
        # Reject traversal segments before prefix matching; a key must remain
        # inside its configured namespace after URL path resolution. Backslashes
        # are treated as separators to cover proxy/filesystem normalization.
        if ".." in path.replace("\\", "/").split("/"):
            return None
        prefixes = _project_media_policy(project_key)[1]
        if not any(path.startswith(prefix) for prefix in prefixes):
            return None
    return value.strip()


def _allowed_origins(project_key: str) -> set[str]:
    return allowed_media_origins(project_key)


def _valid_media_url(value: object, project_key: str, media_kind: str | None = None) -> str | None:
    return valid_media_url(value, project_key, media_kind)


def diagnose_allowed_origins(project_keys: list[str]) -> dict[str, Any]:
    """Return a redacted startup diagnostic for project CDN configuration."""
    configured = {key: len(allowed_media_origins(key)) for key in project_keys}
    missing = sorted(key for key, count in configured.items() if count == 0)
    if missing:
        logger.warning(
            "media allowlist missing configured origins for projects: %s", ",".join(missing)
        )
    return {"configured_origin_counts": configured, "missing_projects": missing}


def _project_public_base(project_key: str) -> str | None:
    origins = allowed_media_origins(project_key)
    return sorted(origins)[0] if origins else None


def _warn_zero_resolve(
    project_key: str, entries: list[dict[str, Any]], resolved: list[dict[str, Any]]
) -> None:
    """Warn when configured registry media contains rejected URLs, without URL data."""
    invalid_count = sum(1 for item in resolved if item.get("url_cdn") is None)
    if invalid_count:
        logger.warning(
            "media entries resolved to zero valid urls: project_key=%s entry_count=%d "
            "invalid_url_count=%d",
            project_key,
            len(entries),
            invalid_count,
        )


def _resolve_media_row(entry: dict[str, Any], project_key: str) -> dict[str, Any]:
    """Build one display-contract dict from a project_config.media entry.

    The DB stores object keys (media/video/..., images/matbang/...) so the seed
    is portable; the public URL is the key prefixed by the configured R2 base.
    """
    return {
        "title": entry.get("title"),
        "kind": entry.get("kind"),
        "url_cdn": _valid_media_url(
            f"{_project_public_base(project_key)}/{entry['object_key']}"
            if entry.get("object_key") and _project_public_base(project_key)
            else None,
            project_key,
            "video",
        ),
        "poster_url": _valid_media_url(
            f"{_project_public_base(project_key)}/{entry['poster_key']}"
            if entry.get("poster_key") and _project_public_base(project_key)
            else None,
            project_key,
            "poster",
        ),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "duration": entry.get("duration"),
        "bytes_mb": entry.get("bytes_mb"),
    }


def _media_from_registry(project_key: str) -> list[dict[str, Any]] | None:
    """Read project_config.media for a project; None on any failure.

    Legacy sync psycopg2 seam for unbound sync callers (see module docstring);
    the async answer path resolves media from the request-bound snapshot
    instead. Best-effort: a short connect timeout keeps a dead DB from
    stalling the caller; any exception returns None so the caller falls back.
    """
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; project media registry unreadable")
        return None
    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT media FROM project_config WHERE project_key = %s AND status = 'active'",
                    (project_key,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        # The row exists: media may legitimately be '[]' (project has no videos
        # yet, e.g. Soleil) — return the empty list rather than falling back to
        # Camellia's bundle, which would mislabel another project's clips.
        entries = [entry for entry in row[0] if isinstance(entry, dict)]
        resolved = [_resolve_media_row(entry, project_key) for entry in entries]
        _warn_zero_resolve(project_key, entries, resolved)
        return resolved
    except Exception:  # noqa: BLE001 — registry read is best-effort
        logger.warning("project_config.media read failed for %s; media unavailable", project_key)
        return None


def list_project_videos(project_key: str = "camellia") -> list[dict[str, Any]]:
    """Return the project video registry for the greeting widget.

    Resolution order (B2: never a sync DB read inside a bound async request):
    1. per-request registry snapshot — entries the async handler already read;
    2. legacy sync psycopg2 read (unbound sync callers only).
    There is deliberately NO static bundle fallback: the Camellia clips under a
    Soleil greeting would be cross-project media, and a missing registry row is
    "no videos for this project", not "borrow Camellia's". The greeting renders
    fine with an empty video list (media is best-effort by contract).
    Callers must treat the result as read-only.
    """
    if request_project_snapshot_bound():
        record = request_project_snapshot()
        if (
            record is not None
            and record.project_key == project_key
            and record.status == "active"
            and record.media_entries is not None
        ):
            # The row exists: media may legitimately be '[]' (project has no
            # videos yet, e.g. Soleil) — return the empty list rather than
            # falling back to Camellia's bundle, which would mislabel another
            # project's clips.
            entries = [entry for entry in record.media_entries if isinstance(entry, dict)]
            resolved = [_resolve_media_row(entry, project_key) for entry in entries]
            _warn_zero_resolve(project_key, entries, resolved)
            return resolved
        return []
    from_registry = _media_from_registry(project_key)
    if from_registry is not None:
        return from_registry
    return []


async def fetch_recent_project_images(project_key: str, limit: int = 8) -> list[dict[str, Any]]:
    """Async recently published gallery rows for a project, no embeddings.

    Fallback path for the greeting when vector search is unavailable (e.g. an
    embedding-provider quota outage): plain published-rows listing scoped by
    project_key through the registry port so the welcome stays decorated
    without any external call or event-loop-blocking read.
    """
    from api.infrastructure.dependencies import get_project_registry  # noqa: PLC0415

    rows = await get_project_registry().fetch_recent_published_images(project_key, limit)
    return [
        {**row, "url_cdn": valid_media_url(row.get("url_cdn"), project_key, "gallery")}
        for row in rows
        if valid_media_url(row.get("url_cdn"), project_key, "gallery")
    ]


def list_project_images(project_key: str, limit: int = 8) -> list[dict[str, Any]]:
    """Legacy sync variant of fetch_recent_project_images (kept for sync CLIs).

    The greeting handler is async and must use the port-backed coroutine; this
    psycopg2 wrapper remains for unbound sync callers that predate the port.
    """
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; greeting image fallback empty")
        return []
    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT image_id, kind, title, caption, alt_text, url_cdn, "
                    "width, height FROM images "
                    "WHERE project_key = %s AND status = 'published' "
                    "ORDER BY updated_at DESC LIMIT %s",
                    (project_key, limit),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — decoration must never 500 a greeting
        logger.warning("greeting image fallback read failed: %s", exc)
        return []
    columns = (
        "image_id",
        "kind",
        "title",
        "caption",
        "alt_text",
        "url_cdn",
        "width",
        "height",
    )
    return [dict(zip(columns, row)) for row in rows]
