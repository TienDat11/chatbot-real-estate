"""Plain-text delivery via https://bot.zapps.me/docs/apis/sendMessage/.

Use the transport directly: AsyncClient's INFO request log exposes the token
in the URL. No redirects, automatic retries, SDK, or provider error logging.
"""

from __future__ import annotations

import re
from contextlib import nullcontext

import httpx

from api.application.ports.zalo_bot import ZaloSendError


class HttpxZaloBotSender:
    """Send once; injected transports remain owned by their caller."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_:~.\-]+", token):
            raise ZaloSendError(code="INVALID_TOKEN", retryable=False)
        self._token = token
        self._transport = transport

    async def send_text(self, chat_id: str, text: str) -> str:
        """Return a confirmed id, otherwise raise a credential-free error.

        A provider acceptance followed by a crash before local id persistence
        can cause duplicate delivery on retry. This adapter does not retry.
        """
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ZaloSendError(code="INVALID_RECIPIENT", retryable=False)
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ZaloSendError(code="INVALID_TEXT", retryable=False)
        invalid_code = None
        try:
            chat_id.encode("utf-8")
        except UnicodeError:
            invalid_code = "INVALID_RECIPIENT"
        try:
            text.encode("utf-8")
        except UnicodeError:
            invalid_code = "INVALID_TEXT"
        if invalid_code is not None:
            raise ZaloSendError(code=invalid_code, retryable=False)

        code, retryable = "SEND_UNAVAILABLE", True
        try:
            request = httpx.Request(
                "POST",
                f"https://bot-api.zaloplatforms.com/bot{self._token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                extensions={"timeout": httpx.Timeout(10.0).as_dict()},
            )
            context = (
                nullcontext(self._transport)
                if self._transport is not None
                else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
            )
            async with context as transport:
                response = await transport.handle_async_request(request)
                try:
                    status = response.status_code
                    if not 200 <= status < 300:
                        if status in (401, 403):
                            code, retryable = "PROVIDER_AUTH_REJECTED", False
                        elif 300 <= status < 500 and status not in (408, 429):
                            code, retryable = "PROVIDER_REJECTED", False
                        raise ZaloSendError()
                    await response.aread()
                    payload = response.json()
                finally:
                    await response.aclose()
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ZaloSendError()
            result = payload.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if (
                not isinstance(message_id, str)
                or not message_id.strip()
                or not message_id.isprintable()
            ):
                raise ZaloSendError()
            return message_id
        except Exception:
            # Provider failures may carry the token URL, response body, or chat id.
            # Raise outside the handler to discard even implicit exception context.
            pass
        raise ZaloSendError(code=code, retryable=retryable) from None
