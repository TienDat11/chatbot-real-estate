"""Application service for durable chat-session history."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from api.application.services.lead_service import mask_phone, validate_phone

# Transcript payload caps (G3-r6 §10.2): a staff transcript body larger than
# 500 messages or 1 MiB answers 413 with NO partial body. The limit is one
# contract, shared by the CRM lead-conversation and training-detail routes.
TRANSCRIPT_MAX_MESSAGES = 500
TRANSCRIPT_MAX_BODY_BYTES = 1024 * 1024

# Citation/image meta allowlist (CRM transcript + training detail): only the
# keys below are ever serialized from a stored message meta; unknown/internal
# fields are dropped at the boundary. Raw phone cannot be in this set, so it
# can never ride out inside a meta object.
SAFE_META_ALLOWED_KEYS = frozenset(
    {"sources", "facts", "images", "citations", "doc_id", "section", "title"}
)

# Candidate scanner for raw Vietnamese mobile numbers buried anywhere inside a
# string value: optional "(" then prefix 0/+84 followed by nine digits with
# short separator runs allowed between them (the "0912 345 678" /
# "(0912)-345-678" shapes). Every candidate is re-validated with the strict
# lead-phone pattern before masking, so prices, dates, and ids that merely
# look numeric never get mangled.
_PHONE_CANDIDATE_RE = re.compile(r"\(?(?:\+84|0)(?:[\s,.()\-]{0,3}\d){9}")


def _mask_phone_candidate(match: re.Match) -> str:
    candidate = match.group(0)
    if candidate.startswith("+"):
        normalized = "+" + re.sub(r"\D", "", candidate[1:])
    else:
        normalized = re.sub(r"\D", "", candidate)
    if validate_phone(normalized):
        return mask_phone(normalized)
    return candidate


def _redact_raw_phones(value: Any) -> Any:
    """Recursively mask raw phone numbers inside arbitrary meta structures.

    The allowlist controls which KEYS survive; this walk guarantees no VALUE
    can smuggle a raw phone inside a nested facts/sources/images structure.
    Masked in place (not dropped) so legitimate non-sensitive metadata stays.
    """
    if isinstance(value, str):
        return _PHONE_CANDIDATE_RE.sub(_mask_phone_candidate, value)
    if isinstance(value, dict):
        return {key: _redact_raw_phones(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_raw_phones(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_raw_phones(item) for item in value)
    return value


def project_safe_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Allowlist-projection of one stored message meta for the wire.

    Two gates, both applied here so no transcript surface can bypass either:
    unknown top-level keys are dropped, then every retained value is walked
    recursively so a raw phone number cannot survive nested inside a
    facts/sources/images structure.
    """
    if not isinstance(meta, dict):
        return None
    safe = {
        key: _redact_raw_phones(value)
        for key, value in meta.items()
        if key in SAFE_META_ALLOWED_KEYS
    }
    return safe or None


def transcript_size_bytes(messages_payload: list[dict[str, Any]]) -> int:
    """Serialized JSON size of a transcript projection (UTF-8)."""
    return len(json.dumps(messages_payload, ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True)
class ChatSessionSummary:
    session_id: str
    device_id: str | None
    project_key: str
    title: str | None
    message_count: int
    handed_off: bool
    last_active_at: datetime
    identity_key: str | None = None


@dataclass(frozen=True)
class TrainingSessionSummary:
    """Private staff training session row (G3-r6).

    ``answer_mode`` is the literal 'training' marker; customer rows can
    never hydrate this projection because the repository predicates are
    mutually exclusive (customer reads require answer_mode IS NULL).
    """

    session_id: str
    owner_firebase_uid: str
    context_project_key: str
    title: str | None
    message_count: int
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str
    meta: dict[str, Any] | None
    created_at: datetime


class ChatHistoryRepository(Protocol):
    async def append_turn(
        self,
        *,
        session_id: str,
        device_id: str | None,
        project_key: str,
        user_content: str,
        assistant_content: str,
        assistant_meta: dict[str, Any],
        identity_key: str | None = None,
    ) -> bool: ...
    async def list_sessions(
        self, *, device_id: str, project_key: str, identity_key: str | None = None, limit: int = 50
    ) -> list[ChatSessionSummary]: ...
    async def get_session(
        self,
        *,
        session_id: str,
        device_id: str | None = None,
        project_key: str | None = None,
        identity_key: str | None = None,
        staff_scoped: bool = False,
    ) -> ChatSessionSummary | None: ...
    async def list_messages(self, *, session_id: str) -> list[ChatMessage]: ...
    async def get_session_for_handoff(
        self,
        *,
        session_id: str,
        project_key: str,
        device_id: str | None = None,
        identity_key: str | None = None,
    ) -> ChatSessionSummary | None: ...
    async def get_session_for_lead(
        self, *, lead_id: int
    ) -> tuple[str, str | None, list[ChatMessage]] | None: ...
    async def mark_handed_off(self, *, session_id: str, lead_id: int) -> None: ...
    async def cleanup(self, *, retention_days: int) -> int: ...
    async def get_cta_state(
        self, *, session_id: str, device_id: str, project_key: str, identity_key: str
    ) -> tuple[int, bool, bool]: ...
    # --- G3-r6 private training history (mutually exclusive with customer) ----
    async def bind_training_session(
        self,
        *,
        session_id: str,
        owner_firebase_uid: str,
        context_project_key: str,
        title: str | None = None,
    ) -> bool: ...
    async def append_training_turn(
        self,
        *,
        session_id: str,
        owner_firebase_uid: str,
        context_project_key: str,
        user_content: str,
        assistant_content: str,
        assistant_meta: dict[str, Any],
        user_message_id: str | None = None,
    ) -> bool: ...
    async def list_training_sessions(
        self,
        *,
        owner_firebase_uid: str | None,
        limit: int,
        cursor_updated_at: datetime | None,
        cursor_session_id: str | None,
        context_project_key: str | None = None,
    ) -> list[TrainingSessionSummary]: ...
    async def get_training_session(
        self, *, session_id: str, owner_firebase_uid: str | None = None
    ) -> TrainingSessionSummary | None: ...
    async def list_training_messages(
        self, *, session_id: str
    ) -> list[tuple[int, ChatMessage]]: ...
    async def get_message_count(self, *, session_id: str) -> int: ...
    async def validate_customer_identity_constraint(self) -> str: ...


class ChatHistoryService:
    def __init__(self, repository: ChatHistoryRepository) -> None:
        self.repository = repository

    async def persist_turn(self, **kwargs: Any) -> bool:
        if not kwargs.get("session_id"):
            return False
        result = await self.repository.append_turn(**kwargs)
        return result is not False

    async def sessions(
        self, *, device_id: str, project_key: str, identity_key: str | None = None
    ) -> list[ChatSessionSummary]:
        return await self.repository.list_sessions(
            device_id=device_id, project_key=project_key, identity_key=identity_key
        )

    async def session(
        self,
        *,
        session_id: str,
        device_id: str | None = None,
        project_key: str | None = None,
        identity_key: str | None = None,
        staff_scoped: bool = False,
    ) -> ChatSessionSummary | None:
        return await self.repository.get_session(
            session_id=session_id,
            device_id=device_id,
            project_key=project_key,
            identity_key=identity_key,
            staff_scoped=staff_scoped,
        )

    async def messages(self, *, session_id: str) -> list[ChatMessage]:
        return await self.repository.list_messages(session_id=session_id)

    async def lead_conversation(
        self, *, lead_id: int
    ) -> tuple[str, str | None, list[ChatMessage]] | None:
        return await self.repository.get_session_for_lead(lead_id=lead_id)

    async def cta_state(
        self, *, session_id: str, device_id: str, project_key: str, identity_key: str
    ) -> tuple[int, bool, bool]:
        return await self.repository.get_cta_state(
            session_id=session_id,
            device_id=device_id,
            project_key=project_key,
            identity_key=identity_key,
        )

    # --- G3-r6 private training history -----------------------------------------

    async def bind_training_session(
        self,
        *,
        session_id: str,
        owner_firebase_uid: str,
        context_project_key: str,
        title: str | None = None,
    ) -> bool:
        """Idempotently bind (owner, session, project context).

        First successful training query creates the row; a repeat converges
        without touching messages and without ever mutating an existing
        CUSTOMER session (the repository predicate rejects foreign rows).
        """
        return await self.repository.bind_training_session(
            session_id=session_id,
            owner_firebase_uid=owner_firebase_uid,
            context_project_key=context_project_key,
            title=title,
        )

    async def persist_training_turn(
        self,
        *,
        session_id: str,
        owner_firebase_uid: str,
        context_project_key: str,
        user_content: str,
        assistant_content: str,
        assistant_meta: dict[str, Any] | None = None,
        user_message_id: str | None = None,
    ) -> bool:
        if not session_id:
            return False
        result = await self.repository.append_training_turn(
            session_id=session_id,
            owner_firebase_uid=owner_firebase_uid,
            context_project_key=context_project_key,
            user_content=user_content,
            assistant_content=assistant_content,
            assistant_meta=assistant_meta or {},
            user_message_id=user_message_id,
        )
        return result is not False

    async def training_sessions(
        self,
        *,
        owner_firebase_uid: str | None,
        limit: int,
        cursor_updated_at: datetime | None,
        cursor_session_id: str | None,
        context_project_key: str | None = None,
    ) -> list[TrainingSessionSummary]:
        return await self.repository.list_training_sessions(
            owner_firebase_uid=owner_firebase_uid,
            limit=limit,
            cursor_updated_at=cursor_updated_at,
            cursor_session_id=cursor_session_id,
            context_project_key=context_project_key,
        )

    async def training_session(
        self, *, session_id: str, owner_firebase_uid: str | None = None
    ) -> TrainingSessionSummary | None:
        return await self.repository.get_training_session(
            session_id=session_id, owner_firebase_uid=owner_firebase_uid
        )

    async def training_messages(self, *, session_id: str) -> list[tuple[int, ChatMessage]]:
        return await self.repository.list_training_messages(session_id=session_id)

    async def message_count(self, *, session_id: str) -> int:
        return await self.repository.get_message_count(session_id=session_id)

    async def validate_customer_identity_constraint(self) -> str:
        return await self.repository.validate_customer_identity_constraint()
