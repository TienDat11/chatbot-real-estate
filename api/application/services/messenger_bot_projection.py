"""Pure projection of Messenger webhook events; no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from api.domain.value_objects.constants import MAX_QUERY_LENGTH

_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")
_SELECT_PREFIX = "SELECT_PROJECT:"


class InboundReason(StrEnum):
    ACCEPTED = "accepted"
    NOT_PAGE_OBJECT = "not_page_object"
    WRONG_PAGE = "wrong_page"
    NO_ENTRY = "no_entry"
    NO_MESSAGING = "no_messaging"
    MISSING_SENDER_ID = "missing_sender_id"
    UNSUPPORTED_EVENT = "unsupported_event"
    ECHO = "echo"
    MISSING_TEXT = "missing_text"
    TEXT_TOO_LONG = "text_too_long"
    BAD_SELECT_PAYLOAD = "bad_select_payload"


@dataclass(frozen=True)
class MessengerInboundEvent:
    psid: str
    event_ref: str
    kind: str
    text: str
    selected_project_key: str | None = None


@dataclass(frozen=True)
class ProjectionResult:
    event: MessengerInboundEvent | None
    reason: InboundReason


def _valid_key(key: str) -> bool:
    return _KEY_RE.fullmatch(key) is not None


def _postback_ref(psid: str, postback: dict) -> str | None:
    mid = postback.get("mid")
    if isinstance(mid, str) and mid:
        return mid
    # Synthetic ref stays stable for the same delivery; timestamp is the
    # only replay-stable field when Meta omits mid.
    ts = postback.get("timestamp")
    if ts is None or ts == "":
        return None
    return f"postback:{psid}:{ts}"


def _project_item(item: object, *, page_id: str | None = None) -> ProjectionResult:
    if not isinstance(item, dict):
        return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)
    sender = item.get("sender")
    psid = sender.get("id") if isinstance(sender, dict) else None
    if not isinstance(psid, str) or not psid:
        return ProjectionResult(None, InboundReason.MISSING_SENDER_ID)
    message = item.get("message")
    # Fixed check order: echo -> page -> sender -> event-kind. Echo is dropped
    # first so a Page never sees its own outbound traffic echoed back.
    if isinstance(message, dict) and message.get("is_echo") is True:
        return ProjectionResult(None, InboundReason.ECHO)
    if page_id:
        # recipient.id is the Page that received the event. Only compare when
        # the field is present: Meta omits it on a few event types, and an
        # absent field must not become a silent drop.
        recipient = item.get("recipient")
        if isinstance(recipient, dict):
            recipient_id = recipient.get("id")
            if isinstance(recipient_id, str) and recipient_id != page_id:
                return ProjectionResult(None, InboundReason.WRONG_PAGE)
    if isinstance(message, dict):
        quick = message.get("quick_reply")
        if isinstance(quick, dict) and "payload" in quick:
            payload = quick.get("payload")
            if payload == "SELECT_PROJECT_MENU":
                mid = message.get("mid")
                if not isinstance(mid, str) or not mid:
                    return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)
                text = message.get("text")
                return ProjectionResult(
                    MessengerInboundEvent(psid, mid, "get_started", text if isinstance(text, str) else ""),
                    InboundReason.ACCEPTED,
                )
            if isinstance(payload, str) and payload.startswith(_SELECT_PREFIX):
                key = payload[len(_SELECT_PREFIX):]
                if not _valid_key(key):
                    return ProjectionResult(None, InboundReason.BAD_SELECT_PAYLOAD)
                mid = message.get("mid")
                if not isinstance(mid, str) or not mid:
                    return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)
                text = message.get("text")
                return ProjectionResult(
                    MessengerInboundEvent(psid, mid, "select_project", text if isinstance(text, str) else "", key),
                    InboundReason.ACCEPTED,
                )
            return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)
        mid = message.get("mid")
        if not isinstance(mid, str) or not mid:
            return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)
        text = message.get("text")
        if not isinstance(text, str) or not text:
            return ProjectionResult(None, InboundReason.MISSING_TEXT)
        if len(text) > MAX_QUERY_LENGTH:
            return ProjectionResult(None, InboundReason.TEXT_TOO_LONG)
        return ProjectionResult(
            MessengerInboundEvent(psid, mid, "message", text),
            InboundReason.ACCEPTED,
        )
    postback = item.get("postback")
    if isinstance(postback, dict):
        payload = postback.get("payload")
        if payload == "GET_STARTED" or payload == "SELECT_PROJECT_MENU":
            ref = _postback_ref(psid, postback)
            if ref is None:
                return ProjectionResult(None, InboundReason.BAD_SELECT_PAYLOAD)
            return ProjectionResult(
                MessengerInboundEvent(psid, ref, "get_started", ""),
                InboundReason.ACCEPTED,
            )
        if isinstance(payload, str) and payload.startswith(_SELECT_PREFIX):
            key = payload[len(_SELECT_PREFIX):]
            if not _valid_key(key):
                return ProjectionResult(None, InboundReason.BAD_SELECT_PAYLOAD)
            ref = _postback_ref(psid, postback)
            if ref is None:
                return ProjectionResult(None, InboundReason.BAD_SELECT_PAYLOAD)
            return ProjectionResult(
                MessengerInboundEvent(psid, ref, "select_project", "", key),
                InboundReason.ACCEPTED,
            )
    return ProjectionResult(None, InboundReason.UNSUPPORTED_EVENT)


def project_messenger_event(
    payload: object, *, page_id: str | None = None
) -> list[ProjectionResult]:
    """Project a webhook body into per-event results.

    Args:
        payload: The parsed webhook JSON body.
        page_id: The Page this app is subscribed to. When set, an entry whose
            ``id`` differs is rejected as WRONG_PAGE. When None (or empty) the
            check is skipped, because an unconfigured Page id cannot be
            distinguished from a mismatch.
    """
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return [ProjectionResult(None, InboundReason.NOT_PAGE_OBJECT)]
    entry = payload.get("entry")
    if not isinstance(entry, list) or not entry:
        return [ProjectionResult(None, InboundReason.NO_ENTRY)]
    expected_page_id = page_id.strip() if isinstance(page_id, str) else None
    out: list[ProjectionResult] = []
    for ent in entry:
        if expected_page_id:
            entry_id = ent.get("id") if isinstance(ent, dict) else None
            if not isinstance(entry_id, str) or entry_id != expected_page_id:
                out.append(ProjectionResult(None, InboundReason.WRONG_PAGE))
                continue
        messaging = ent.get("messaging") if isinstance(ent, dict) else None
        if not isinstance(messaging, list) or not messaging:
            out.append(ProjectionResult(None, InboundReason.NO_MESSAGING))
            continue
        for item in messaging:
            out.append(_project_item(item, page_id=expected_page_id))
    return out
