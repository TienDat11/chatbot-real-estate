"""Stable private-chat correlation; identities do not grant sales authorization."""

from __future__ import annotations

import hmac
from uuid import NAMESPACE_URL, uuid5

_IDENTITY_NAMESPACE = "zalo-bot:identity:v1:"
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


def global_session_id(identity_key: str) -> str:
    """Derive a UUID-compatible global session in a separate stable domain."""
    if not isinstance(identity_key, str) or not identity_key:
        raise ValueError("Invalid Zalo Bot opaque identity")
    try:
        return str(uuid5(_SESSION_NAMESPACE, identity_key))
    except UnicodeError:
        raise ValueError("Invalid Zalo Bot opaque identity") from None
