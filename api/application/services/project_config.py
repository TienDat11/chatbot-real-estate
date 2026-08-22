"""Project identity from the project_config registry (stories 8.2 + 10.2).

Single registry source the answer path reads for per-project identity fields
(ten_thuong_mai, ten_phap_ly, vi_tri, hotline) and the brand token used by the
deterministic intent classifier. Story 10.2: prompts, greeting, and conv
directives are parameterized with {project_*} placeholders and rendered at
runtime against this registry, so a second active project stops inheriting
Camellia's name and location.

Best-effort by contract: every read is synchronous with a short connect timeout
and degrades to the Camellia defaults below, so a dead DB never crashes the
answer path and pre-project callers behave exactly as before.
"""

from __future__ import annotations

import logging
import re
from typing import Any

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


def fetch_project_identity(project_key: str | None = None) -> dict[str, str]:
    """Return identity fields for a project; Camellia defaults on any failure.

    ``project_key`` None resolves to DEFAULT_PROJECT_KEY so legacy callers keep
    the Camellia identity. The registry row is authoritative; when it is missing
    or the DB is unreachable the static Camellia snapshot is returned so prompt
    rendering never crashes.
    """
    key = project_key or DEFAULT_PROJECT_KEY
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; using default project identity")
        return dict(_CAMELLIA_IDENTITY)
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
            return dict(_CAMELLIA_IDENTITY)
        return {
            "ten_thuong_mai": row[0] or _CAMELLIA_IDENTITY["ten_thuong_mai"],
            "ten_phap_ly": row[1] or _CAMELLIA_IDENTITY["ten_phap_ly"],
            "vi_tri": row[2] or _CAMELLIA_IDENTITY["vi_tri"],
            "hotline": row[3] or _CAMELLIA_IDENTITY["hotline"],
        }
    except Exception as exc:  # noqa: BLE001 — registry read is best-effort
        logger.warning("project_config identity read failed (%s); using defaults", exc)
        return dict(_CAMELLIA_IDENTITY)


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


__all__ = [
    "DEFAULT_PROJECT_KEY",
    "fetch_project_identity",
    "render_template",
    "brand_token",
]
