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
            raise ZaloSendError("Invalid Zalo Bot token")
        self._token = token
        self._transport = transport

    async def send_text(self, chat_id: str, text: str) -> str:
        """Return a confirmed id, otherwise raise a credential-free error.

        A provider acceptance followed by a crash before local id persistence
        can cause duplicate delivery on retry. This adapter does not retry.
        """
        if not isinstance(chat_id, str) or not chat_id.strip():
            raise ZaloSendError("Invalid Zalo Bot recipient")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ZaloSendError("Invalid Zalo Bot text")

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
                    if not 200 <= response.status_code < 300:
                        raise ZaloSendError("Zalo Bot delivery failed")
                    await response.aread()
                    payload = response.json()
                finally:
                    await response.aclose()
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise ZaloSendError("Zalo Bot delivery failed")
            result = payload.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if (
                not isinstance(message_id, str)
                or not message_id.strip()
                or not message_id.isprintable()
            ):
                raise ZaloSendError("Zalo Bot delivery failed")
            return message_id
        except Exception:
            # Provider failures may carry the token URL, response body, or chat id.
            # Raise outside the handler to discard even implicit exception context.
            pass
        raise ZaloSendError("Zalo Bot delivery failed") from None
