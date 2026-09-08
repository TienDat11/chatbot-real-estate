"""Shared offline seams for the G3-r6 training contract (underscore: not collected).

Training /query now passes a preflight (context registry gate + owner-bound
durable session) and persists through the PRIVATE training path. These fakes
swap exactly the two production seams the preflight and the persist block
resolve lazily at call time:

- ``api.application.services.project_config.load_project_registry_record``
- ``api.infrastructure.adapters.postgres_chat_history.repository``

so every training test stays offline: no registry DB, no chat DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

TRAINING_TEST_UID = "uid-sales-mapped"


class FakeTrainingChatRepository:
    """Minimal chat-history repository fake for the training surfaces."""

    def __init__(self) -> None:
        # session_id -> (owner_firebase_uid, context_project_key)
        self.bound: dict[str, tuple[str, str]] = {}
        self.training_turns: list[dict[str, Any]] = []
        self.customer_append_calls = 0

    async def bind_training_session(
        self, *, session_id: str, owner_firebase_uid: str, context_project_key: str, title=None
    ) -> bool:
        existing = self.bound.get(session_id)
        if existing is not None and existing != (owner_firebase_uid, context_project_key):
            return False
        self.bound[session_id] = (owner_firebase_uid, context_project_key)
        return True

    async def get_training_session(
        self, *, session_id: str, owner_firebase_uid: str | None = None
    ):
        existing = self.bound.get(session_id)
        if existing is None:
            return None
        if owner_firebase_uid is not None and existing[0] != owner_firebase_uid:
            return None
        return SimpleNamespace(
            session_id=session_id,
            owner_firebase_uid=existing[0],
            context_project_key=existing[1],
            title=None,
            message_count=0,
            created_at=None,
            last_active_at=None,
        )

    async def append_training_turn(self, **kwargs) -> bool:
        session = self.bound.get(kwargs.get("session_id"))
        if session is None or session[0] != kwargs.get("owner_firebase_uid"):
            return False
        self.training_turns.append(kwargs)
        return True

    async def append_turn(self, **kwargs) -> bool:
        # Proves training never touches the customer append path.
        self.customer_append_calls += 1
        return True


def training_payload(**overrides) -> dict:
    """A schema-valid G3-r6 training /query body (synthetic values only)."""
    payload: dict[str, Any] = {
        "query": "coaching",
        "answer_mode": "training",
        "session_id": "s-training-1",
        "context": {"project_key": "camellia"},
    }
    payload.update(overrides)
    return payload


def install_training_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: FakeTrainingChatRepository | None = None,
    statuses: dict[str, str] | None = None,
) -> FakeTrainingChatRepository:
    """Patch the registry + chat-history seams; return the chat fake."""
    repo = repo or FakeTrainingChatRepository()
    statuses = {"camellia": "active", **statuses} if statuses else {"camellia": "active"}

    async def _fake_record(project_key: str | None):
        if not project_key:
            return None
        status = statuses.get(project_key)
        if status is None:
            return None
        return SimpleNamespace(project_key=project_key, status=status)

    monkeypatch.setattr(
        "api.application.services.project_config.load_project_registry_record", _fake_record
    )
    monkeypatch.setattr(
        "api.infrastructure.adapters.postgres_chat_history.repository", repo
    )
    return repo


__all__ = [
    "TRAINING_TEST_UID",
    "FakeTrainingChatRepository",
    "install_training_seams",
    "training_payload",
]
