"""Training-history contract primitives (G3-r6 spec §10.2, ISSUE-G4-03).

Shared by the sales training routes and the /query training branch: the
opaque keyset cursor codec, the context-project validation rules, and the
transcript projection used by the detail endpoint. Kept in the application
layer so the wire contract can never drift between the two surfaces.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timezone
from typing import Any

from api.application.services.chat_history_service import (
    TRANSCRIPT_MAX_MESSAGES,
    ChatMessage,
    project_safe_meta,
)

# The training detail body is capped by the SAME transcript contract as the
# CRM lead conversation (500 messages / 1 MiB); this alias names the training
# surface of that shared cap.
TRAINING_MESSAGE_HARD_CAP = TRANSCRIPT_MAX_MESSAGES

# Same key shape the lead/query surfaces accept for project keys.
_PROJECT_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

TRAINING_RESERVED_PROJECT_KEYS = frozenset({"_training"})

# project_config.status value that marks a live project (same predicate the
# registry adapter's active-projects query uses).
ACTIVE_PROJECT_STATUS = "active"


class TrainingContextRequiredError(ValueError):
    """Training mode without exactly one valid context project key (422)."""


class TrainingProjectInactiveError(Exception):
    """Named context project exists but is no longer active (409)."""


class TrainingProjectUnknownError(ValueError):
    """Named context project is not in the registry (422/404 by route)."""


def encode_training_cursor(updated_at: datetime, session_id: str) -> str:
    """Opaque base64url keyset token mirroring (updated_at DESC, id DESC)."""
    micros = int(updated_at.timestamp() * 1_000_000)
    raw = f"{micros}|{session_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_training_cursor(value: str) -> tuple[datetime, str]:
    """Decode a cursor; raises ValueError on anything malformed (route 400)."""
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        micros_text, session_id = raw.split("|", 1)
        micros = int(micros_text)
        if micros < 0 or not session_id:
            raise ValueError("training cursor out of range")
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("malformed training cursor") from exc
    moment = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
    return moment, session_id


def require_training_context_key(project_key: object) -> str:
    """Validate the training context key shape; raises the 422-side error."""
    if not isinstance(project_key, str) or not _PROJECT_KEY_PATTERN.fullmatch(project_key):
        raise TrainingContextRequiredError(
            "training mode requires exactly one valid context.project_key"
        )
    if project_key in TRAINING_RESERVED_PROJECT_KEYS:
        raise TrainingContextRequiredError("reserved project key cannot be a training context")
    return project_key


def validate_training_project(project_key: str, *, status: str | None) -> None:
    """Registry gate for the training context project.

    ``status`` is the registry row's status (None = no row / degraded read).
    No row -> TrainingProjectUnknownError (422 side); row present but not
    'active' -> TrainingProjectInactiveError (spec: 409 "Project context is
    no longer active"). The caller reads the row through the cached
    ``load_project_registry_record`` seam — no extra port method needed.
    """
    if status is None:
        raise TrainingProjectUnknownError(f"unknown project_key: {project_key}")
    if status != ACTIVE_PROJECT_STATUS:
        raise TrainingProjectInactiveError("Project context is no longer active")


def filter_training_messages_for_response(
    rows: list[tuple[int, ChatMessage]],
) -> list[dict[str, Any]]:
    """Project stored training rows onto the wire transcript shape.

    Meta is allowlist-projected (sources/facts/images only) so no internal
    field — and structurally no raw phone — can ride out of a training body.
    """
    payload: list[dict[str, Any]] = []
    for message_id, message in rows:
        entry: dict[str, Any] = {
            "id": str(message_id),
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
        }
        safe_meta = project_safe_meta(message.meta)
        if safe_meta is not None:
            entry["meta"] = safe_meta
        payload.append(entry)
    return payload


__all__ = [
    "TRAINING_MESSAGE_HARD_CAP",
    "TrainingContextRequiredError",
    "TrainingProjectInactiveError",
    "TrainingProjectUnknownError",
    "encode_training_cursor",
    "filter_training_messages_for_response",
    "parse_training_cursor",
    "require_training_context_key",
    "validate_training_project",
]
