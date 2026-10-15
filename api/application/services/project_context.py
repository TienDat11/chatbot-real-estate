"""Immutable project identity and answer-mode snapshot for one pipeline turn."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from api.application.services.project_scope import (
    RESERVED_PROJECT_KEYS,
    validate_project_key,
)


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """Validated real project scope plus the exact active catalogue snapshot."""

    project_key: str
    answer_mode: Literal["normal", "training"]
    active_catalogue: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.answer_mode not in ("normal", "training"):
            raise ValueError("answer_mode must be 'normal' or 'training'")
        try:
            validate_project_key(self.project_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid project context key: {self.project_key!r}") from exc
        if self.project_key in RESERVED_PROJECT_KEYS or self.project_key == "_global":
            raise ValueError(f"reserved project context key: {self.project_key}")
        catalogue = tuple(tuple(item) for item in self.active_catalogue)
        if not catalogue:
            raise ValueError("active_catalogue must not be empty")
        if any(
            len(item) != 2
            or not isinstance(item[0], str)
            or not item[0].strip()
            or not isinstance(item[1], str)
            or not item[1].strip()
            for item in catalogue
        ):
            raise ValueError("active_catalogue entries require non-blank key and name")
        if self.project_key not in {key for key, _ in catalogue}:
            raise ValueError(f"project {self.project_key!r} is not active")
        object.__setattr__(self, "active_catalogue", catalogue)


__all__ = ["ProjectContext"]
