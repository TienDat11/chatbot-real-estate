"""Firebase Cloud Messaging HTTP v1 sender; credentials stay in environment."""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt

from api.application.ports.fcm_notifications import FcmSender, FcmTokenInvalidError

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# FCM signals a dead registration token with 403 (v1 reason UNREGISTERED),
# legacy 410, or INVALID_ARGUMENT for malformed tokens; all are permanent.
_INVALID_TOKEN_STATUSES = frozenset({403, 404, 410})
_INVALID_TOKEN_MARKERS = (
    "registration-token-not-registered",
    "unregistered",
    "invalid_registration",
    "invalid registration token",
)


def _is_invalid_token_response(response: httpx.Response) -> bool:
    if response.status_code not in _INVALID_TOKEN_STATUSES:
        return False
    lowered = (response.text or "").lower()
    return any(marker in lowered for marker in _INVALID_TOKEN_MARKERS)


class FirebaseFcmSender(FcmSender):
    def __init__(self, *, project_id: str, client_email: str, private_key: str) -> None:
        self.project_id = project_id
        self.client_email = client_email
        self.private_key = private_key.replace("\\n", "\n")

    async def _access_token(self) -> str:
        if not self.project_id or not self.client_email or not self.private_key:
            raise RuntimeError("Firebase FCM credentials are not configured")
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self.client_email,
                "scope": _SCOPE,
                "aud": _TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            },
            self.private_key,
            algorithm="RS256",
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            response.raise_for_status()
            return str(response.json()["access_token"])

    async def send(
        self, *, token: str, title: str | None, body: str | None, data: dict[str, str]
    ) -> None:
        access_token = await self._access_token()
        # The custom service worker navigates on data.url; fcm_options.link is
        # kept identical so the default click path lands on the same screen.
        target_url = data.get("url") or "/"
        # Data-only messages (title is None) are the only reliable way to force
        # the SW onBackgroundMessage path for background delivery verification,
        # so the top-level "notification" / "webpush.notification" blocks are
        # omitted entirely — FCM still accepts "webpush":{"fcm_options":{"link"}}.
        message: dict[str, Any] = {"token": token, "data": data}
        if title is not None:
            message["notification"] = {"title": title, "body": body}
            message["webpush"] = {
                "notification": {"title": title, "body": body},
                "fcm_options": {"link": target_url},
            }
        else:
            # Keep the click-through link even for data-only payloads; the
            # notification block is intentionally absent, not empty.
            message["webpush"] = {"fcm_options": {"link": target_url}}
        payload: dict[str, Any] = {"message": message}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
            if _is_invalid_token_response(response):
                raise FcmTokenInvalidError(
                    f"FCM rejected the registration token (status {response.status_code})"
                )
            response.raise_for_status()


__all__ = ["FirebaseFcmSender"]
