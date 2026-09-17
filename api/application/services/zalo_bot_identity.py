"""Stable private-chat correlation; identities do not grant sales authorization."""

from __future__ import annotations

import hmac
import json
from uuid import NAMESPACE_URL, uuid5

_IDENTITY_NAMESPACE = "zalo-bot:identity:v1:"
_INGRESS_NAMESPACE = "zalo-bot:ingress:v1:"
_SESSION_NAMESPACE = uuid5(NAMESPACE_URL, "zalo-bot:session:v1:")


def opaque_identity(chat_id: str, secret: str) -> str:
    """Derive a namespaced HMAC using an independent, stable identity secret.

    Rotating this key changes bindings and session ownership; it requires an
    approved re-provisioning/history-continuity procedure, not automatic merge.
    """
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("Invalid Zalo Bot chat identity")
    if not isinstance(secret, str) or not 32 <= len(secret) <= 256:
        raise ValueError("Invalid Zalo Bot identity secret")
    try:
        key = secret.encode("utf-8")
        message = (_IDENTITY_NAMESPACE + chat_id).encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid Zalo Bot identity input") from None
    return _IDENTITY_NAMESPACE + hmac.digest(key, message, "sha256").hex()


def ingress_key(chat_id: str, message_id: str, identity_secret: str) -> str:
    """Hash an exact routing pair in a domain separate from actor identity."""
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("Invalid Zalo Bot ingress identity")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Invalid Zalo Bot ingress message")
    if not isinstance(identity_secret, str) or not 32 <= len(identity_secret) <= 256:
        raise ValueError("Invalid Zalo Bot identity secret")
    try:
        key = identity_secret.encode("utf-8")
        pair = json.dumps([chat_id, message_id], ensure_ascii=False, separators=(",", ":"))
        message = (_INGRESS_NAMESPACE + pair).encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid Zalo Bot ingress input") from None
    return _INGRESS_NAMESPACE + hmac.digest(key, message, "sha256").hex()


def global_session_id(identity_key: str) -> str:
    """Derive a UUID-compatible global session in a separate stable domain."""
    if not isinstance(identity_key, str) or not identity_key:
        raise ValueError("Invalid Zalo Bot opaque identity")
    try:
        return str(uuid5(_SESSION_NAMESPACE, identity_key))
    except UnicodeError:
        raise ValueError("Invalid Zalo Bot opaque identity") from None
