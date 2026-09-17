"""Pure projection of supported Zalo private text events; no I/O."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ZaloInboundTextEvent:
    chat_id: str
    message_id: str
    text: str
    event_name: str


def project_zalo_private_text_event(parsed: object) -> ZaloInboundTextEvent | None:
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return None
    result = parsed.get("result")
    if not isinstance(result, dict):
        return None
    event_name = result.get("event_name")
    if event_name != "message.text.received":
        return None
    message = result.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("chat_type") != "PRIVATE":
        return None
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    text = message.get("text")
    if not all(isinstance(value, str) and value for value in (chat_id, message_id, text)):
        return None
    return ZaloInboundTextEvent(chat_id, message_id, text, event_name)
