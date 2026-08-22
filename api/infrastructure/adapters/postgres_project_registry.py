"""Postgres adapter for the project registry port (B2/M1/m8 fix).

One implementation of the registry read that five hand-rolled sync psycopg2
blocks used to duplicate (identity, catalogue, media, gallery fallback, geo
center). Connections are made with explicit ``asyncpg.connect`` kwargs —
never an interpolated DSN string — so a leaked exception can never carry the
password (m8). Each call is best-effort with a short connect timeout and
degrades to None/[] instead of raising: the registry is a garnish the answer
path must survive losing.
"""

from __future__ import annotations

import logging
from typing import Any

from api.application.ports.project_registry import (
    ProjectRegistryPort,
    ProjectRegistryRecord,
)
from api.infrastructure.config.config import get_settings

logger = logging.getLogger("api.postgres_project_registry")

# Short connect timeout: a dead DB must stall the answer path for at most
# this long before the caller degrades to its static defaults.
_CONNECT_TIMEOUT_S = 2.0

_PROJECT_ROW_QUERY = """
SELECT project_key, ten_thuong_mai, ten_phap_ly, vi_tri, hotline,
       COALESCE(location, vi_tri) AS location,
       geo_center_lat, geo_center_lng, is_hot, status, media
FROM project_config
WHERE project_key = $1
"""

_ACTIVE_PROJECTS_QUERY = """
SELECT project_key, ten_thuong_mai, ten_phap_ly, vi_tri, hotline,
       COALESCE(location, vi_tri) AS location,
       geo_center_lat, geo_center_lng, is_hot, status, media
FROM project_config
WHERE status = 'active'
ORDER BY is_hot DESC, ten_thuong_mai
"""

_RECENT_IMAGES_QUERY = """
SELECT image_id, kind, title, caption, alt_text, url_cdn, width, height
FROM images
WHERE project_key = $1 AND status = 'published'
ORDER BY updated_at DESC
LIMIT $2
"""

_IMAGE_COLUMNS = (
    "image_id", "kind", "title", "caption", "alt_text", "url_cdn", "width", "height",
)


class PostgresProjectRegistry(ProjectRegistryPort):
    """asyncpg-backed registry reads; one short-lived connection per call."""

    async def _connect(self):
        import asyncpg

        s = get_settings()
        # kwargs (not a DSN string) so the password can never leak through an
        # exception message that embeds the connection target (m8).
        return await asyncpg.connect(
            host=s.postgres_host,
            port=s.postgres_port,
            user=s.postgres_user,
            password=s.postgres_password,
            database=s.postgres_database,
            timeout=_CONNECT_TIMEOUT_S,
        )

    async def fetch_project(self, project_key: str) -> ProjectRegistryRecord | None:
        """Return the registry row for one project; None on miss or failure."""
        if not project_key:
            return None
        try:
            conn = await self._connect()
            try:
                row = await conn.fetchrow(_PROJECT_ROW_QUERY, project_key)
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 — registry read is best-effort
            logger.warning("registry: project row read failed for %s: %s", project_key, exc)
            return None
        if row is None:
            logger.warning("registry: no project_config row for %s", project_key)
            return None
        return ProjectRegistryRecord(
            project_key=row["project_key"],
            ten_thuong_mai=row["ten_thuong_mai"],
            ten_phap_ly=row["ten_phap_ly"],
            vi_tri=row["vi_tri"],
            hotline=row["hotline"],
            location=row["location"],
            geo_center_lat=(
                float(row["geo_center_lat"]) if row["geo_center_lat"] is not None else None
            ),
            geo_center_lng=(
                float(row["geo_center_lng"]) if row["geo_center_lng"] is not None else None
            ),
            is_hot=bool(row["is_hot"]),
            status=row["status"] or "",
            media_entries=list(row["media"]) if row["media"] is not None else None,
        )

    async def fetch_active_projects(self) -> list[ProjectRegistryRecord]:
        """Return every active registry row; [] on any failure (degraded)."""
        try:
            conn = await self._connect()
            try:
                rows = await conn.fetch(_ACTIVE_PROJECTS_QUERY)
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 — scope resolution must degrade
            logger.warning("registry: active projects read failed: %s", exc)
            return []
        return [
            ProjectRegistryRecord(
                project_key=r["project_key"],
                ten_thuong_mai=r["ten_thuong_mai"],
                ten_phap_ly=r["ten_phap_ly"],
                vi_tri=r["vi_tri"],
                hotline=r["hotline"],
                location=r["location"],
                geo_center_lat=(
                    float(r["geo_center_lat"]) if r["geo_center_lat"] is not None else None
                ),
                geo_center_lng=(
                    float(r["geo_center_lng"]) if r["geo_center_lng"] is not None else None
                ),
                is_hot=bool(r["is_hot"]),
                status=r["status"] or "",
            )
            for r in rows
        ]

    async def fetch_recent_published_images(
        self, project_key: str, limit: int
    ) -> list[dict[str, Any]]:
        """Return recently published gallery rows for a project; [] on failure."""
        try:
            conn = await self._connect()
            try:
                rows = await conn.fetch(_RECENT_IMAGES_QUERY, project_key, limit)
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 — decoration must never 500 a greeting
            logger.warning("registry: recent images read failed for %s: %s", project_key, exc)
            return []
        return [dict(zip(_IMAGE_COLUMNS, row)) for row in rows]


__all__ = ["PostgresProjectRegistry"]
