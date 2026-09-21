"""Pure projection of supported Zalo private text events; no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from api.domain.value_objects.constants import MAX_QUERY_LENGTH


class InboundReason(StrEnum):
    ACCEPTED = "accepted"
    NOT_OBJECT = "not_object"
    OK_NOT_TRUE = "ok_not_true"
    MISSING_RESULT = "missing_result"
    UNSUPPORTED_EVENT = "unsupported_event"
    MISSING_MESSAGE = "missing_message"
    NON_PRIVATE_CHAT = "non_private_chat"
    MISSING_CHAT_ID = "missing_chat_id"
    MISSING_MESSAGE_ID = "missing_message_id"
    MISSING_TEXT = "missing_text"
    TEXT_TOO_LONG = "text_too_long"


@dataclass(frozen=True)
class ProjectionResult:
    event: ZaloInboundTextEvent | None
    reason: InboundReason


@dataclass(frozen=True)
class ZaloInboundTextEvent:
    chat_id: str
    message_id: str
    text: str
    event_name: str


def project_zalo_private_text_event(parsed: object) -> ProjectionResult:
    if not isinstance(parsed, dict):
        return ProjectionResult(None, InboundReason.NOT_OBJECT)
    if parsed.get("ok") is not True:
        return ProjectionResult(None, InboundReason.OK_NOT_TRUE)
    result = parsed.get("result")
    if not isinstance(result, dict):
        return ProjectionResult(None, InboundReason.MISSING_RESULT)
    event_name = result.get("event_name")
    if event_name != "message.text.received":
        return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)
    message = result.get("message")
    if not isinstance(message, dict):
        return ProjectionResult(None, InboundReason.MISSING_MESSAGE)
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("chat_type") != "PRIVATE":
        return ProjectionResult(None, InboundReason.NON_PRIVATE_CHAT)
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    text = message.get("text")
    if not isinstance(chat_id, str) or not chat_id:
        return ProjectionResult(None, InboundReason.MISSING_CHAT_ID)
    if not isinstance(message_id, str) or not message_id:
        return ProjectionResult(None, InboundReason.MISSING_MESSAGE_ID)
    if not isinstance(text, str) or not text:
        return ProjectionResult(None, InboundReason.MISSING_TEXT)
    if len(text) > MAX_QUERY_LENGTH:
        return ProjectionResult(None, InboundReason.TEXT_TOO_LONG)
    return ProjectionResult(
        ZaloInboundTextEvent(chat_id, message_id, text, event_name),
        InboundReason.ACCEPTED,
    )
