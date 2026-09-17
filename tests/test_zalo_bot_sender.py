"""Offline Zalo sendMessage contract and credential-leak regression coverage."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import traceback

import httpx
import pytest

from api.application.ports.zalo_bot import ZaloSendError
from api.infrastructure.adapters.zalo_bot_sender import HttpxZaloBotSender

TOKEN = "123456:" + "sender-secret-marker"
CHAT_ID = "private-chat-marker"
TEXT = "Xin chào khách hàng"


@pytest.mark.parametrize("text", [TEXT, "ế" * 2000])
def test_send_message_request_and_confirmed_id(text, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == (
            f"https://bot-api.zaloplatforms.com/bot{TOKEN}/sendMessage"
        )
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"chat_id": CHAT_ID, "text": text}
        timeout = request.extensions["timeout"]
        assert set(timeout) == {"connect", "read", "write", "pool"}
        assert all(math.isfinite(value) and value > 0 for value in timeout.values())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": "msg-123"}})

    sender = HttpxZaloBotSender(TOKEN, transport=httpx.MockTransport(handle))
    assert asyncio.run(sender.send_text(CHAT_ID, text)) == "msg-123"
    assert len(requests) == 1
    assert TOKEN not in caplog.text
    assert CHAT_ID not in caplog.text
    assert text not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"ok": False, "description": TOKEN},
        {"ok": 1, "result": {"message_id": "msg-123"}},
        {"ok": "true", "result": {"message_id": "msg-123"}},
        {"ok": True},
        {"ok": True, "result": []},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": None}},
        {"ok": True, "result": {"message_id": 123}},
        {"ok": True, "result": {"message_id": ""}},
        {"ok": True, "result": {"message_id": " "}},
        {"ok": True, "result": {"message_id": "msg\n123"}},
    ],
)
def test_invalid_response_never_confirms_delivery(payload, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    sender = HttpxZaloBotSender(
        TOKEN,
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(ZaloSendError) as caught:
        asyncio.run(sender.send_text(CHAT_ID, TEXT))
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "SEND_UNAVAILABLE"
    assert caught.value.retryable is True
    assert len(requests) == 1


def test_invalid_json_is_safe(caplog):
    caplog.set_level(logging.DEBUG)
    sender = HttpxZaloBotSender(
        TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=TOKEN.encode())
        ),
    )
    with pytest.raises(ZaloSendError) as caught:
        asyncio.run(sender.send_text(CHAT_ID, TEXT))
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "SEND_UNAVAILABLE"
    assert caught.value.retryable is True


@pytest.mark.parametrize("status,code,retryable", [
    (302, "PROVIDER_REJECTED", False),
    (400, "PROVIDER_REJECTED", False),
    (401, "PROVIDER_AUTH_REJECTED", False),
    (403, "PROVIDER_AUTH_REJECTED", False),
    (408, "SEND_UNAVAILABLE", True),
    (429, "SEND_UNAVAILABLE", True),
    (500, "SEND_UNAVAILABLE", True),
    (599, "SEND_UNAVAILABLE", True),
])
def test_http_failure_and_redirect_are_not_retried(status, code, retryable, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://example.invalid/redirect"},
            json={"ok": True, "result": {"message_id": "must-not-accept"}, "error": TOKEN},
        )

    sender = HttpxZaloBotSender(TOKEN, transport=httpx.MockTransport(handle))
    with pytest.raises(ZaloSendError) as caught:
        asyncio.run(sender.send_text(CHAT_ID, TEXT))
    assert len(requests) == 1
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == code
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, RuntimeError])
def test_secret_transport_exception_has_no_chain_or_log(error_type, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        raise error_type(f"{request.url} {CHAT_ID} {TEXT}")

    sender = HttpxZaloBotSender(TOKEN, transport=httpx.MockTransport(handle))
    with pytest.raises(ZaloSendError) as caught:
        asyncio.run(sender.send_text(CHAT_ID, TEXT))
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "SEND_UNAVAILABLE"
    assert caught.value.retryable is True
    assert len(requests) == 1


def test_stream_failure_closes_response_without_leaking(caplog):
    caplog.set_level(logging.DEBUG)

    class FailingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'{"ok":'
            raise httpx.ReadTimeout(TOKEN)

        async def aclose(self):
            self.closed = True

    stream = FailingStream()
    sender = HttpxZaloBotSender(
        TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream)
        ),
    )
    with pytest.raises(ZaloSendError) as caught:
        asyncio.run(sender.send_text(CHAT_ID, TEXT))
    assert stream.closed
    _assert_private_failure(caught.value, caplog)
    assert caught.value.retryable is True


@pytest.mark.parametrize("token", ["", " ", "a/b", "a\\b", "a?b", "a#b", "a%2fb", "a\nb", "a\x7fb"])
def test_invalid_token_fails_without_disclosure(token, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ZaloSendError) as caught:
        HttpxZaloBotSender(token)
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "INVALID_TOKEN"
    assert caught.value.retryable is False


@pytest.mark.parametrize("chat_id,text,code", [
    (CHAT_ID, "", "INVALID_TEXT"),
    (CHAT_ID, " ", "INVALID_TEXT"),
    (CHAT_ID, "x" * 2001, "INVALID_TEXT"),
    (CHAT_ID, "private-text-\ud800", "INVALID_TEXT"),
    (CHAT_ID, {}, "INVALID_TEXT"),
    ("", TEXT, "INVALID_RECIPIENT"),
    ("chat-\ud800", TEXT, "INVALID_RECIPIENT"),
    (123, TEXT, "INVALID_RECIPIENT"),
])
def test_invalid_message_never_reaches_transport(chat_id, text, code):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": "bad"}})

    sender = HttpxZaloBotSender(TOKEN, transport=httpx.MockTransport(handle))
    with pytest.raises(ZaloSendError) as caught:
        asyncio.run(sender.send_text(chat_id, text))
    assert requests == []
    assert caught.value.code == code
    assert caught.value.retryable is False


def _assert_private_failure(error, caplog):
    rendered = "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    for private in (TOKEN, CHAT_ID, TEXT):
        assert private not in str(error)
        assert private not in rendered
        assert private not in caplog.text


def test_cancellation_is_not_wrapped_or_retried():
    requests = []

    def handle(request):
        requests.append(request)
        raise asyncio.CancelledError()

    sender = HttpxZaloBotSender(TOKEN, transport=httpx.MockTransport(handle))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sender.send_text(CHAT_ID, TEXT))
    assert len(requests) == 1
