"""Per-project video registry for the greeting widget (story 8.2 / ISSUE-01).

The registry source moved from a frozen module constant to the
``project_config.media`` column so each project carries its own R2 video bundle
(Soleil vs Camellia differ). Each DB entry stores R2 *object keys* plus display
metadata; the public URL is resolved against ``settings.r2_public_base`` at call
time so one row works across environments (dev vs prod R2 hosts), mirroring the
key-based convention of ``ingest/upload_videos_r2.py``.

WHY DB now and not a literal: the multi-project registry (project_config) is the
single place an operator adds a project; hardcoding the bundle here again would
fork the truth the moment a second project ships videos. The static tuple below
survives ONLY as a best-effort fallback so the greeting latch never 500s when
the registry row is missing or the DB is unreachable (matches the previous
"frozen config, no I/O" contract — callers treat the result as read-only).

Callers that predate project_key (api/interfaces/api/hello.py) call with no
arguments; the Camellia default keeps them working until ISSUE-05 scopes the
call sites by project.
"""

from __future__ import annotations

import logging
from typing import Any

from api.infrastructure.config.config import settings

logger = logging.getLogger("api.media_config")

# Public R2 base resolved at import: settings.r2_public_base honors a custom
# domain when configured and otherwise falls back to the account-derived
# pub-<account_id>.r2.dev host, so the fallback never hardcodes an environment.
_public_base = settings.r2_public_base

# Poster frame: the project overview render already served for the greeting.
_POSTER = f"{_public_base}/images/matbang/matbang-02.png"

# Ordered for the widget: light weight first, drone full-motion second.
# Camellia's legacy bundle — used only when project_config has no row yet.
VIDEO_MEDIA: tuple[dict[str, Any], ...] = (
    {
        "title": "The Camellia - Brand Film (Web)",
        "kind": "brand",
        "url_cdn": f"{_public_base}/media/video/brand-film-web.mp4",
        "poster_url": _POSTER,
        "width": 1920,
        "height": 1080,
        "duration": None,
        "bytes_mb": None,
    },
    {
        "title": "The Camellia - Brand Film (Original)",
        "kind": "brand",
        "url_cdn": f"{_public_base}/media/video/brand-film-faststart.mp4",
        "poster_url": _POSTER,
        "width": 1920,
        "height": 1080,
        "duration": None,
        "bytes_mb": None,
    },
    {
        "title": "The Camellia - Drone Overview (DJI)",
        "kind": "drone",
        "url_cdn": f"{_public_base}/media/video/dji-orbit-faststart.mp4",
        "poster_url": _POSTER,
        "width": None,
        "height": None,
        "duration": None,
        "bytes_mb": None,
    },
)

# Display contract the frontend consumes; extra DB fields are dropped here.


def _resolve_media_row(entry: dict[str, Any]) -> dict[str, Any]:
    """Build one display-contract dict from a project_config.media entry.

    The DB stores object keys (media/video/..., images/matbang/...) so the seed
    is portable; the public URL is the key prefixed by the configured R2 base.
    """
    return {
        "title": entry.get("title"),
        "kind": entry.get("kind"),
        "url_cdn": f"{_public_base}/{entry['object_key']}" if entry.get("object_key") else None,
        "poster_url": f"{_public_base}/{entry['poster_key']}" if entry.get("poster_key") else None,
        "width": entry.get("width"),
        "height": entry.get("height"),
        "duration": entry.get("duration"),
        "bytes_mb": entry.get("bytes_mb"),
    }


def _media_from_registry(project_key: str) -> list[dict[str, Any]] | None:
    """Read project_config.media for a project; None on any failure.

    Sync psycopg2 because every caller is synchronous (the greeting latch).
    Best-effort: a short connect timeout keeps a dead DB from stalling the
    greeting; any exception returns None so the caller falls back.
    """
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; using static video registry")
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
        return [_resolve_media_row(entry) for entry in row[0] if isinstance(entry, dict)]
    except Exception as exc:  # noqa: BLE001 — registry read is best-effort
        logger.warning("project_config.media read failed (%s); using static registry", exc)
        return None


def list_project_videos(project_key: str = "camellia") -> list[dict[str, Any]]:
    """Return the project video registry for the greeting widget.

    Best-effort and synchronous: prefers the project's ``project_config.media``
    rows, falls back to the static Camellia bundle when the registry is missing
    or unreachable so the greeting always has videos to attach. Callers must
    treat the result as read-only.
    """
    from_registry = _media_from_registry(project_key)
    if from_registry is not None:
        return from_registry
    return [dict(item) for item in VIDEO_MEDIA]
