"""Page-scoped text delivery via the Messenger Platform Send API.

Use the transport directly: AsyncClient's INFO request log exposes the
access token in the URL. No redirects, automatic retries, SDK, or
provider error logging.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import nullcontext
from urllib.parse import urlencode

import httpx

from api.application.ports.messenger_bot import MessengerSendError

logger = logging.getLogger(__name__)

# Transient Graph codes: delivery unknown, safe to retry via the outbox.
# 2022 stays out: Meta disables the app after a policy violation and documents no
# recovery action, so a retry cannot succeed and only burns outbox attempts.
_RETRYABLE_GRAPH_CODES = frozenset({1, 2, 4, 17, 341, 613, 2018321})
# Permanent Graph codes: permission/parameter failures, retry cannot help.
_REJECTED_GRAPH_CODES = frozenset(
    {10, 100, 102, 551, 10900, 10903, 10904, 2018300} | set(range(200, 300))
)


class HttpxMessengerBotSender:
    """Send once; injected transports remain owned by their caller."""

    def __init__(
        self,
        *,
        page_id: str,
        page_access_token: str,
        api_base_url: str,
        api_version: str,
        app_secret: str = "",
        appsecret_proof_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if (
            not isinstance(page_id, str)
            or not page_id.isdigit()
            or not isinstance(page_access_token, str)
            or not page_access_token.strip()
        ):
            raise MessengerSendError(code="INVALID_TOKEN", retryable=False)
        self._page_id = page_id
        self._token = page_access_token
        self._base_url = api_base_url.rstrip("/") if isinstance(api_base_url, str) else api_base_url
        self._api_version = api_version
        self._app_secret = app_secret
        self._proof_enabled = appsecret_proof_enabled
        self._transport = transport

    async def send_text(self, psid: str, text: str) -> str:
        """Return the confirmed message id, otherwise raise a credential-free error.

        Provider acceptance followed by a crash before local id persistence
        can duplicate delivery on retry. This adapter never retries.
        """
        self._check_recipient(psid)
        self._check_text(text)
        return await self._post(
            {
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {"text": text},
            }
        )

    async def send_quick_replies(
        self, psid: str, text: str, options: list[tuple[str, str]]
    ) -> str:
        """Send text with tappable quick replies; each option is (title, payload)."""
        self._check_recipient(psid)
        self._check_text(text)
        self._check_options(options)
        return await self._post(
            {
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {
                    "text": text,
                    "quick_replies": [
                        {"content_type": "text", "title": title, "payload": payload}
                        for title, payload in options
                    ],
                },
            }
        )

    def _url(self) -> str:
        query = [("access_token", self._token)]
        if self._proof_enabled:
            proof = hmac.new(
                self._app_secret.encode("utf-8"),
                self._token.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            query.append(("appsecret_proof", proof))
        return f"{self._base_url}/{self._api_version}/{self._page_id}/messages?{urlencode(query)}"

    @staticmethod
    def _check_recipient(psid: str) -> None:
        if not isinstance(psid, str) or not psid.strip():
            raise MessengerSendError(code="INVALID_RECIPIENT", retryable=False)
        try:
            psid.encode("utf-8")
        except UnicodeError:
            raise MessengerSendError(code="INVALID_RECIPIENT", retryable=False)

    @staticmethod
    def _check_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise MessengerSendError(code="INVALID_TEXT", retryable=False)
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise MessengerSendError(code="INVALID_TEXT", retryable=False)

    @staticmethod
    def _check_options(options: list[tuple[str, str]]) -> None:
        if not isinstance(options, list) or not options or len(options) > 13:
            raise MessengerSendError(code="INVALID_TEXT", retryable=False)
        for option in options:
            if (
                not isinstance(option, (tuple, list))
                or len(option) != 2
                or not isinstance(option[0], str)
                or not option[0].strip()
                or len(option[0]) > 20
                or not isinstance(option[1], str)
                or not option[1].strip()
                or len(option[1]) > 1000
            ):
                raise MessengerSendError(code="INVALID_TEXT", retryable=False)
            try:
                option[0].encode("utf-8")
                option[1].encode("utf-8")
            except UnicodeError:
                raise MessengerSendError(code="INVALID_TEXT", retryable=False)

    async def _post(self, body: dict) -> str:
        # Transport failure leaves delivery unknown, so it stays retryable;
        # every mapped provider verdict below overrides this default.
        code, retryable = "SEND_UNAVAILABLE", True
        try:
            request = httpx.Request(
                "POST",
                self._url(),
                json=body,
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
                    await response.aread()
                    try:
                        payload = response.json()
                    except Exception:
                        payload = None
                finally:
                    await response.aclose()
            if 200 <= status < 300:
                message_id = payload.get("message_id") if isinstance(payload, dict) else None
                if (
                    isinstance(message_id, str)
                    and message_id.strip()
                    and message_id.isprintable()
                ):
                    return message_id
                code, retryable = "SEND_UNAVAILABLE", False
                raise MessengerSendError()
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            error_code = error.get("code")
            if isinstance(error_code, bool):
                error_code = None
            # Read once: both the debug line and the recipient pair below need it.
            error_subcode = error.get("error_subcode")
            if isinstance(error_subcode, bool):
                error_subcode = None
            if error:
                fbtrace_id = error.get("fbtrace_id")
                logger.debug(
                    "messenger send failed: code=%s subcode=%s fbtrace_id=%s",
                    error_code if isinstance(error_code, int) else None,
                    error_subcode
                    if isinstance(error_subcode, int) and not isinstance(error_subcode, bool)
                    else None,
                    fbtrace_id if isinstance(fbtrace_id, str) else None,
                )
            if status in (401, 403):
                code, retryable = "PROVIDER_AUTH_REJECTED", False
            elif error_code == 100 and error_subcode == 2018001:
                # "No matching user found": the psid is unusable, not malformed.
                # Checked before the code sets because 100 alone is a generic
                # invalid-parameter code that must stay PROVIDER_REJECTED.
                code, retryable = "INVALID_RECIPIENT", False
            elif isinstance(error_code, int) and error_code in _RETRYABLE_GRAPH_CODES:
                code, retryable = "SEND_UNAVAILABLE", True
            elif error_code == 190:
                code, retryable = "PROVIDER_AUTH_REJECTED", False
            elif isinstance(error_code, int) and error_code in _REJECTED_GRAPH_CODES:
                code, retryable = "PROVIDER_REJECTED", False
            elif status in (408, 429) or 500 <= status < 600:
                code, retryable = "SEND_UNAVAILABLE", True
            elif 300 <= status < 500:
                code, retryable = "PROVIDER_REJECTED", False
            else:
                code, retryable = "SEND_UNAVAILABLE", False
            raise MessengerSendError()
        except Exception:
            # Provider failures may carry the token URL, response body, or psid.
            # Raise outside the handler to discard even implicit exception context.
            pass
        raise MessengerSendError(code=code, retryable=retryable) from None
