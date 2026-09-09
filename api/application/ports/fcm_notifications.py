"""Ports for FCM token registration and delivery.

Identity is (identity_type, identity_key): staff authenticate with a Firebase
uid, while anonymous chat customers authenticate with the same signed anon
token /api/lead verifies — there is no Firebase Auth on the customer side.
"""

from __future__ import annotations

from typing import Protocol

IDENTITY_TYPE_FIREBASE = "firebase_uid"
IDENTITY_TYPE_ANON_CUSTOMER = "anon_customer"


class FcmTokenInvalidError(Exception):
    """Raised by a sender when FCM reports the registration token is dead.

    Covers UNREGISTERED / 410 / INVALID_ARGUMENT answers: the token can never
    receive again, so the service prunes it instead of retrying.
    """


class FcmTokenRepository(Protocol):
    async def register_fcm_token(
        self, *, identity_type: str, identity_key: str, token: str, platform: str
    ) -> None: ...

    async def remove_fcm_token(
        self, *, identity_type: str, identity_key: str, token: str
    ) -> None: ...

    async def list_fcm_tokens(self, *, identity_type: str, identity_key: str) -> list[str]: ...

    async def list_fcm_tokens_for_sales(self, *, sales_id: int) -> list[str]: ...

    async def prune_token(self, *, token: str) -> None: ...


class FcmSender(Protocol):
    # title/body are Optional: a None pair yields a data-only message that
    # deterministically exercises the service-worker onBackgroundMessage path
    # (used to verify background delivery without a notification payload).
    async def send(
        self, *, token: str, title: str | None, body: str | None, data: dict[str, str]
    ) -> None:
        """Deliver one message; raise FcmTokenInvalidError for dead tokens."""
        ...


__all__ = [
    "IDENTITY_TYPE_ANON_CUSTOMER",
    "IDENTITY_TYPE_FIREBASE",
    "FcmSender",
    "FcmTokenInvalidError",
    "FcmTokenRepository",
]
