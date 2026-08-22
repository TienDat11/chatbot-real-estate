"""Outbound port: the per-project registry the answer path reads from.

Everything the greeting / answer pipeline needs to know about the active
project — identity fields (ten_thuong_mai, vi_tri, ...), geo center, and the
media bundle — comes from one registry read through this port. Application
services depend on this contract only; the concrete Postgres adapter lives in
``api/infrastructure/adapters/postgres_project_registry.py`` (dependency
inversion: the adapter imports the port, never the reverse).

Every method is best-effort by contract: a dead DB returns ``None`` / ``[]``
so the caller can degrade to its static defaults instead of crashing the
request (B2/M1: one async read per request replaces five hand-rolled sync
psycopg2 blocks that blocked the event loop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProjectRegistryRecord:
    """One ``project_config`` row, shaped for the answer path.

    ``media_entries`` is the raw JSONB list (R2 object keys + display
    metadata); ``media_config`` resolves it against the public R2 base.
    ``None`` means the column is NULL or the row was not read.
    """

    project_key: str
    ten_thuong_mai: str | None
    ten_phap_ly: str | None
    vi_tri: str | None
    hotline: str | None
    location: str | None
    geo_center_lat: float | None
    geo_center_lng: float | None
    is_hot: bool
    status: str
    media_entries: list[dict[str, Any]] | None = field(default=None)

    @property
    def geo_center(self) -> tuple[float, float] | None:
        """Return the (lat, lng) pair when BOTH coordinates are present."""
        if self.geo_center_lat is None or self.geo_center_lng is None:
            return None
        return (float(self.geo_center_lat), float(self.geo_center_lng))


class ProjectRegistryPort(Protocol):
    """Registry read contract used by identity/media/geo/scope consumers."""

    async def fetch_project(self, project_key: str) -> ProjectRegistryRecord | None:
        """Return the registry row for one project; None when missing/unreadable."""
        ...

    async def fetch_active_projects(self) -> list[ProjectRegistryRecord]:
        """Return every active registry row; [] on any failure (degraded)."""
        ...

    async def fetch_recent_published_images(
        self, project_key: str, limit: int
    ) -> list[dict[str, Any]]:
        """Return recently published gallery rows for a project (no embeddings)."""
        ...


__all__ = ["ProjectRegistryRecord", "ProjectRegistryPort"]
