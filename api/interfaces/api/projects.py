"""Project catalogue endpoint for the FE project picker (story 10.3).

GET /api/projects returns the active project list (HOT first, then name) so the
picker popup shows real detailed Vietnamese addresses and leads with Camellia.
Best-effort by contract, same as fetch_project_identity: a dead DB degrades to
``projects: []`` (200) so the picker never breaks and falls back to its static
catalogue. Pure pass-through — the registry service owns the read + ordering.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
async def list_projects() -> dict:
    """Return the active project catalogue; ``projects: []`` on any failure."""
    from api.application.services.project_config import fetch_projects  # noqa: PLC0415

    return {"projects": fetch_projects()}
