"""Project scope resolution for chat/lead requests (stories 10.1 + 10.4).

Single place that turns a request's optional ``project_key`` into the active
project every downstream leg must read from. Story 10.1 [RV-22/08]: when the
client does not pick a project the backend must NOT guess — with more than one
active project a 422 tells the frontend to show the ProjectPicker, and with
exactly one active project that project is the safe default.

Reserved keys (D5, ISSUE-01): ``_legacy`` (untagged corpus awaiting review) and
``_training`` (training namespace, filter-2-layers with kind='training') are
never offered as a client-scoped project; they are only readable through
dedicated namespaces.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from api.infrastructure.config.config import settings

logger = logging.getLogger("api.project_scope")

# Story 8.5/G3: publish endpoint validates the same pattern — path-traversal
# guard plus a bounded, URL-safe namespace shape.
PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9_]{2,40}$")

# D5 reserved namespaces — real projects must never use a leading underscore.
RESERVED_PROJECT_KEYS = frozenset({"_legacy", "_training"})

# HTTP-level signal for the default-rule failure; mapped to 422 by handlers.
PROJECT_CHOICE_REQUIRED = "Vui lòng chọn dự án (có nhiều dự án đang mở bán)"


class ProjectScopeError(ValueError):
    """Raised when a project_key is invalid or the active-project rule fails."""


@dataclass(frozen=True)
class ActiveProject:
    """One row of project_config where status='active'."""

    project_key: str
    ten_thuong_mai: str


def validate_project_key(project_key: str) -> None:
    """Validate the project_key shape; raise ProjectScopeError when invalid.

    Reserved keys are rejected here too: the query/lead API is customer-facing
    and must never let a caller read the _legacy/_training corpora.
    """
    if not project_key:
        raise ProjectScopeError("project_key là bắt buộc")
    if not PROJECT_KEY_PATTERN.fullmatch(project_key):
        raise ProjectScopeError("project_key không hợp lệ (a-z0-9_, 2-40 ký tự)")
    if project_key in RESERVED_PROJECT_KEYS:
        raise ProjectScopeError(f"project_key '{project_key}' là key dành riêng")


async def fetch_active_projects() -> list[ActiveProject]:
    """Return all active project_config rows; empty on any failure (degraded).

    Best-effort with a short timeout: a dead DB must not take down /query or
    /api/lead — the caller applies the default-rule on whatever comes back.
    """
    import asyncpg

    dsn = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}"
    )
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
        try:
            rows = await conn.fetch(
                "SELECT project_key, ten_thuong_mai FROM project_config WHERE status = 'active'"
            )
        finally:
            await conn.close()
        return [ActiveProject(r["project_key"], r["ten_thuong_mai"]) for r in rows]
    except Exception as exc:  # noqa: BLE001 — scope resolution must degrade, never crash
        logger.warning("project_scope: active projects read failed: %s", exc)
        return []


async def resolve_project_key(
    requested: str | None,
    *,
    active_projects: list[ActiveProject] | None = None,
) -> str:
    """Resolve the effective project_key for one request (default rule 10.1).

    - ``requested`` set: validate shape + reserved keys, then require the project
      to be active (an explicit inactive key is an error).
    - ``requested`` None: exactly one active project -> that project; zero or
      more than one -> ProjectScopeError so the frontend can prompt for a choice.

    ``active_projects`` is injectable for tests; defaults to the DB read.
    """
    if requested:
        validate_project_key(requested)
        projects = active_projects if active_projects is not None else await fetch_active_projects()
        if not any(p.project_key == requested for p in projects):
            raise ProjectScopeError(f"Dự án '{requested}' không hoạt động hoặc không tồn tại")
        return requested

    projects = active_projects if active_projects is not None else await fetch_active_projects()
    if len(projects) == 1:
        return projects[0].project_key
    if len(projects) > 1:
        raise ProjectScopeError(PROJECT_CHOICE_REQUIRED)
    raise ProjectScopeError("Chưa có dự án nào đang mở bán")


async def filter_images_by_project(
    images: list[dict], project_key: str
) -> list[dict]:
    """Keep only image dicts whose image_id belongs to the project (story 10.4).

    ``images`` comes from the media lane's search (api/application/services/
    image_search.py), which has no project filter. images.project_key is the
    per-image tag written by the ingest lane; images that have no tag yet must
    not surface for any project (isolation first: a missing tag must never leak
    across projects), so they are dropped here.
    """
    if not images:
        return []
    import asyncpg

    dsn = (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_database}"
    )
    ids = [img.get("image_id") for img in images if img.get("image_id")]
    if not ids:
        return []
    try:
        conn = await asyncpg.connect(dsn, timeout=2.0)
        try:
            rows = await conn.fetch(
                "SELECT image_id FROM images WHERE image_id = ANY($1::text[]) "
                "AND project_key = $2 AND status = 'published'",
                ids,
                project_key,
            )
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001 — image enrichment must degrade, never crash
        logger.warning("project_scope: image project filter failed: %s", exc)
        return []
    allowed = {r["image_id"] for r in rows}
    return [img for img in images if img.get("image_id") in allowed]


__all__ = [
    "PROJECT_KEY_PATTERN",
    "RESERVED_PROJECT_KEYS",
    "PROJECT_CHOICE_REQUIRED",
    "ProjectScopeError",
    "ActiveProject",
    "validate_project_key",
    "fetch_active_projects",
    "resolve_project_key",
    "filter_images_by_project",
]
