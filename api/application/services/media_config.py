"""Static project video registry for the greeting widget.

Production masters are far too heavy for web playback (1129 MB DJI remux needs
no transcode; the 589 MB brand film needs a light web copy). Rather than stand
up a videos table now, the demo lists the processed clips uploaded to R2 via a
small immutable config. Each entry is a display contract for the frontend: a
poster frame, a playable mp4, and metadata the UI can use for aspect/ratio.

WHY static config and not a DB: this is a temporary demo surface that carries
three hand-curated objects. A dedicated table + ingest step is the right shape
once videos become first-class data; until then a literal list keeps the
greeting latch simple and avoids touching the schema. Order matters: the most
web-appropriate clip (light brand film) leads so the widget never defaults to
a 1.1 GB download.
"""

from __future__ import annotations

from typing import Any

from api.infrastructure.config.config import settings

# Public R2 base resolved at import: settings.r2_public_base honors a custom
# domain when configured and otherwise falls back to the account-derived
# pub-<account_id>.r2.dev host, so this registry never hardcodes an environment.
_public_base = settings.r2_public_base

# Poster frame: the project overview render already served for the greeting.
_POSTER = f"{_public_base}/images/matbang/matbang-02.png"

# Ordered for the widget: light weight first, drone full-motion second.
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


def list_project_videos() -> list[dict[str, Any]]:
    """Return the project video registry for the greeting widget.

    Best-effort and synchronous: returns the frozen config list unchanged so
    the greeting latch always has videos to attach without any I/O or failure
    path. Callers must treat the result as read-only.
    """
    return [dict(item) for item in VIDEO_MEDIA]
