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

import asyncio
import logging
import threading
from typing import Any

import asyncpg

from api.application.ports.project_registry import (
    ProjectRegistryPort,
    ProjectRegistryRecord,
)
from api.application.services.media_config import valid_media_url
from api.infrastructure.config.config import get_settings
from api.application.services.sql_leg import _pooler_safe_kwargs

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
    "image_id",
    "kind",
    "title",
    "caption",
    "alt_text",
    "url_cdn",
    "width",
    "height",
)


class PostgresProjectRegistry(ProjectRegistryPort):
    """asyncpg-backed registry reads using one reusable pool per event loop.

    asyncpg pools (and the transports underneath them) are bound to the event
    loop that created them. The registry adapter is a process-wide singleton,
    so it can be called from different loops over its lifetime — notably under
    Starlette's ``TestClient`` when the fixture does not ``with``-enter the
    client, which spins up and closes a fresh portal/event loop per request.
    A single cached pool would then be force-terminated after its loop was
    closed; on Windows the closed loop's proactor is ``None`` and the teardown
    raises ``'NoneType' object has no attribute 'send'`` (a lifecycle bug, not
    a DB failure, that the best-effort ``except`` used to hide as a degraded
    read). Keeping one pool per loop means a pool is only ever used and
    closed on its own loop; pools from already-closed loops are dropped by
    reference — their loop teardown already released the sockets.
    """

    def __init__(self) -> None:
        self._pools: dict[int, tuple[asyncio.AbstractEventLoop, asyncpg.Pool]] = {}
        self._pool_locks: dict[int, asyncio.Lock] = {}
        # asyncio.Lock is loop-bound and cannot guard state shared across
        # loops, so the pool map is protected by a short critical-section
        # threading lock (dict reads/writes only — no blocking on IO).
        self._pools_guard = threading.Lock()

    def _cached_pool(self, running: asyncio.AbstractEventLoop) -> asyncpg.Pool | None:
        """Return the running loop's pool when it is still usable."""
        with self._pools_guard:
            entry = self._pools.get(id(running))
        if entry is None:
            return None
        loop, pool = entry
        if loop is not running or pool.is_closing():
            return None
        return pool

    def _drop_dead_pools(self) -> None:
        """Forget pools whose event loop has been closed (best-effort sweep).

        A closed loop has already released its transports, so the pool needs
        no teardown — keeping the entry would only leak it in long-running
        processes that see many short-lived loops (e.g. test clients).
        """
        with self._pools_guard:
            dead = [key for key, (loop, _pool) in self._pools.items() if loop.is_closed()]
            for key in dead:
                self._pools.pop(key, None)
                self._pool_locks.pop(key, None)

    async def _get_pool(self) -> asyncpg.Pool:
        running = asyncio.get_running_loop()
        cached = self._cached_pool(running)
        if cached is not None:
            return cached
        # One async lock per loop so concurrent tasks on the same loop do not
        # create two pools; the lock is created on this loop and only ever
        # awaited from it.
        lock = self._pool_locks.get(id(running))
        if lock is None:
            lock = asyncio.Lock()
            with self._pools_guard:
                self._pool_locks[id(running)] = lock
        async with lock:
            cached = self._cached_pool(running)
            if cached is not None:
                return cached
            self._drop_dead_pools()
            s = get_settings()
            pool = await asyncpg.create_pool(
                host=s.postgres_host,
                port=s.postgres_port,
                user=s.postgres_user,
                password=s.postgres_password,
                database=s.postgres_database,
                min_size=1,
                max_size=max(1, min(int(s.postgres_max_connections), 10)),
                timeout=_CONNECT_TIMEOUT_S,
                command_timeout=_CONNECT_TIMEOUT_S,
                **_pooler_safe_kwargs(),
            )
            with self._pools_guard:
                self._pools[id(running)] = (running, pool)
            return pool

    async def close(self) -> None:
        """Close the current loop's pool and forget pools from closed loops.

        Shutdown-only, best-effort: a pool whose loop is already closed needs
        no action (its sockets were released with the loop), and a live pool
        belonging to a different loop is never touched from here.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        with self._pools_guard:
            entries = list(self._pools.items())
        for key, (loop, pool) in entries:
            if loop.is_closed():
                with self._pools_guard:
                    self._pools.pop(key, None)
                    self._pool_locks.pop(key, None)
            elif running is not None and loop is running and not pool.is_closing():
                try:
                    await pool.close()
                except Exception:  # noqa: BLE001 — shutdown must never raise
                    logger.warning("registry: pool close failed on shutdown", exc_info=True)
                finally:
                    with self._pools_guard:
                        self._pools.pop(key, None)
                        self._pool_locks.pop(key, None)

    async def fetch_project(self, project_key: str) -> ProjectRegistryRecord | None:
        """Return the registry row for one project; None on miss or failure."""
        if not project_key:
            return None
        try:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(_PROJECT_ROW_QUERY, project_key)
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
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(_ACTIVE_PROJECTS_QUERY)
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
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(_RECENT_IMAGES_QUERY, project_key, limit)
        except Exception as exc:  # noqa: BLE001 — decoration must never 500 a greeting
            logger.warning("registry: recent images read failed for %s: %s", project_key, exc)
            return []
        images: list[dict[str, Any]] = []
        for row in rows:
            image = dict(zip(_IMAGE_COLUMNS, row))
            # "gallery" kind check matches media_config.fetch_recent_project_images:
            # without the kind, only the origin is validated and a same-host URL
            # outside the project's object-key namespace would pass.
            image["url_cdn"] = valid_media_url(image.get("url_cdn"), project_key, "gallery")
            if image["url_cdn"]:
                images.append(image)
        return images


__all__ = ["PostgresProjectRegistry"]
