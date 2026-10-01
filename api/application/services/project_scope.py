"""Project scope resolution for chat/lead requests (stories 10.1 + 10.4).

Single place that turns a request's optional ``project_key`` into the active
project every downstream leg must read from. Story 10.1 [RV-22/08]: when the
client does not pick a project the backend must NOT guess — with more than one
active project a 422 tells the frontend to show the ProjectPicker, and with
exactly one active project that project is the safe default.

Reserved keys (D5, ISSUE-01): ``_legacy`` (untagged corpus awaiting review) is
never offered as a client-scoped project; it is only readable through a
dedicated namespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from api.application.ports.project_registry import ProjectRegistryRecord

# guard plus a bounded, URL-safe namespace shape.
PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9_]{2,40}$")

# Training prompts use ProjectContext.answer_mode; the reserved marker remains
# rejected as a client-supplied project key.
TRAINING_PROJECT_KEY = "_training"

# Reserved keys (D5, ISSUE-01): ``_legacy`` (untagged corpus awaiting review)
# is never offered as a client-scoped project. ``_training`` is additionally
# rejected client-side because it is a server-internal mode marker: a real
# training turn always carries its context project explicitly, so nothing
# downstream may ever treat the marker itself as a corpus scope.
RESERVED_PROJECT_KEYS = frozenset({"_legacy", TRAINING_PROJECT_KEY})

# HTTP-level signal for the default-rule failure; mapped to 422 by handlers.
PROJECT_CHOICE_REQUIRED = "Vui lòng chọn dự án (có nhiều dự án đang mở bán)"


class ProjectScopeError(ValueError):
    """Raised when a project_key is invalid or the active-project rule fails."""







@dataclass(frozen=True)
class ActiveProject:
    project_key: str
    ten_thuong_mai: str


def validate_project_key(project_key: str) -> None:
    """Validate the project_key shape; raise ProjectScopeError when invalid.

    Reserved keys are rejected here too: the query/lead API is customer-facing
    and must never let a caller read the _legacy corpus or spoof the internal
    training mode marker as a client-named scope.
    """
    if not project_key:
        raise ProjectScopeError("project_key là bắt buộc")
    if not PROJECT_KEY_PATTERN.fullmatch(project_key):
        raise ProjectScopeError("project_key không hợp lệ (a-z0-9_, 2-40 ký tự)")
    if project_key in RESERVED_PROJECT_KEYS:
        raise ProjectScopeError(f"project_key '{project_key}' là key dành riêng")


async def fetch_active_project_records() -> list[ProjectRegistryRecord]:
    """Fetch full active rows for request snapshots needing picker metadata."""
    from api.infrastructure.dependencies import get_project_registry  # noqa: PLC0415

    return await get_project_registry().fetch_active_projects()


async def fetch_active_projects() -> list[ActiveProject]:
    """Return active project identities used by scope resolution."""
    records = await fetch_active_project_records()
    return [ActiveProject(row.project_key, row.ten_thuong_mai or "") for row in records]


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


__all__ = [
    "PROJECT_KEY_PATTERN",
    "RESERVED_PROJECT_KEYS",
    "PROJECT_CHOICE_REQUIRED",
    "TRAINING_PROJECT_KEY",
    "ProjectScopeError",
    "ActiveProject",
    "validate_project_key",
    "fetch_active_projects",
    "fetch_active_project_records",
]
