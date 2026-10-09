"""Offline Messenger Send API contract and credential-leak regression coverage."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import traceback

import httpx
import pytest

from api.application.ports.messenger_bot import MessengerSendError
from api.infrastructure.adapters.messenger_bot_sender import HttpxMessengerBotSender

PAGE_ID = "123456789012345"
TOKEN = "page-access-token-marker"
PSID = "recipient-psid-marker"
TEXT = "Xin chào quý khách"
BASE_URL = "https://graph.facebook.com"
API_VERSION = "v26.0"
APP_SECRET = "app-secret-marker"


def _sender(handle, **kwargs):
    params = {
        "page_id": PAGE_ID,
        "page_access_token": TOKEN,
        "api_base_url": BASE_URL,
        "api_version": API_VERSION,
        "transport": httpx.MockTransport(handle),
    }
    params.update(kwargs)
    return HttpxMessengerBotSender(**params)


@pytest.mark.parametrize("text", [TEXT, "ế" * 2000])
def test_send_text_request_and_confirmed_id(text, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/{API_VERSION}/{PAGE_ID}/messages"
        assert request.url.params["access_token"] == TOKEN
        assert "appsecret_proof" not in request.url.params
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "recipient": {"id": PSID},
            "messaging_type": "RESPONSE",
            "message": {"text": text},
        }
        timeout = request.extensions["timeout"]
        assert set(timeout) == {"connect", "read", "write", "pool"}
        assert all(math.isfinite(value) and value > 0 for value in timeout.values())
        return httpx.Response(200, json={"message_id": "mid.123"})

    assert asyncio.run(_sender(handle).send_text(PSID, text)) == "mid.123"
    assert len(requests) == 1
    assert TOKEN not in caplog.text
    assert text not in caplog.text


def test_send_quick_replies_boundary_ok(caplog):
    caplog.set_level(logging.DEBUG)
    options = [(f"t{'x' * 19}", f"payload-{i}") for i in range(13)]
    options[0] = ("t" * 20, "payload-0")
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["message"]["text"] == TEXT
        assert body["message"]["quick_replies"] == [
            {"content_type": "text", "title": title, "payload": payload}
            for title, payload in options
        ]
        return httpx.Response(200, json={"message_id": "mid.qr"})

    assert asyncio.run(_sender(handle).send_quick_replies(PSID, TEXT, options)) == "mid.qr"
    assert len(requests) == 1
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "options",
    [
        [(f"title-{i}", f"p-{i}") for i in range(14)],
        [],
        [("t" * 21, "p")],
        [("", "p")],
        [("t", "")],
        [("t", "p" * 1001)],
    ],
)
def test_invalid_quick_replies_never_reach_transport(options):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message_id": "bad"})

    with pytest.raises(MessengerSendError) as caught:
        asyncio.run(_sender(handle).send_quick_replies(PSID, TEXT, options))
    assert requests == []
    assert caught.value.code == "INVALID_TEXT"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "status,error,code,retryable",
    [
        (400, {"code": 190, "error_subcode": 492, "fbtrace_id": "abc"}, "PROVIDER_AUTH_REJECTED", False),
        (400, {"code": 4, "error_subcode": 130, "fbtrace_id": "abc"}, "SEND_UNAVAILABLE", True),
        (400, {"code": 2018321, "fbtrace_id": "abc"}, "SEND_UNAVAILABLE", True),
        (400, {"code": 1, "fbtrace_id": "abc"}, "SEND_UNAVAILABLE", True),
        (400, {"code": 100, "error_subcode": 33, "fbtrace_id": "abc"}, "PROVIDER_REJECTED", False),
        (400, {"code": 100, "error_subcode": 2018001, "fbtrace_id": "abc"}, "INVALID_RECIPIENT", False),
        (400, {"code": 2022, "fbtrace_id": "abc"}, "PROVIDER_REJECTED", False),
        (400, {"code": 10, "error_subcode": 2534022, "fbtrace_id": "abc"}, "PROVIDER_REJECTED", False),
        (400, {"code": 10, "error_subcode": 2018278, "fbtrace_id": "abc"}, "PROVIDER_REJECTED", False),
        (400, {"code": 200, "fbtrace_id": "abc"}, "PROVIDER_REJECTED", False),
        (401, None, "PROVIDER_AUTH_REJECTED", False),
        (403, {"code": 10, "fbtrace_id": "abc"}, "PROVIDER_AUTH_REJECTED", False),
        (408, None, "SEND_UNAVAILABLE", True),
        (429, None, "SEND_UNAVAILABLE", True),
        (500, None, "SEND_UNAVAILABLE", True),
        (302, None, "PROVIDER_REJECTED", False),
        (400, None, "PROVIDER_REJECTED", False),
        (200, None, "SEND_UNAVAILABLE", False),
    ],
)
def test_graph_error_map(status, error, code, retryable, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []
    body = {"error": {**error, "message": TOKEN, "error_data": TEXT}} if error else {"ok": True}

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    with pytest.raises(MessengerSendError) as caught:
        asyncio.run(_sender(handle).send_text(PSID, TEXT))
    assert len(requests) == 1
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == code
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("payload", [None, [], {}, {"message_id": None}, {"message_id": ""}, {"message_id": " "}, {"message_id": "mid\n1"}])
def test_invalid_success_never_confirms_delivery(payload, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MessengerSendError) as caught:
        asyncio.run(
            _sender(lambda request: httpx.Response(200, json=payload)).send_text(PSID, TEXT)
        )
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "SEND_UNAVAILABLE"


def test_no_token_or_text_in_log(caplog):
    caplog.set_level(logging.DEBUG)
    body = {"error": {"message": f"{TOKEN} {TEXT} {PSID}", "code": 4, "error_subcode": 130, "fbtrace_id": "trace-1"}}

    def handle(request):
        return httpx.Response(400, json=body)

    with pytest.raises(MessengerSendError) as caught:
        asyncio.run(_sender(handle).send_text(PSID, TEXT))
    assert caught.value.code == "SEND_UNAVAILABLE"
    assert caught.value.retryable is True
    assert TOKEN not in caplog.text
    assert TEXT not in caplog.text
    assert PSID not in caplog.text


def test_appsecret_proof_query():
    requests = []
    expected = hmac.new(
        APP_SECRET.encode("utf-8"), TOKEN.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message_id": "mid.proof"})

    sender = _sender(handle, app_secret=APP_SECRET, appsecret_proof_enabled=True)
    assert asyncio.run(sender.send_text(PSID, TEXT)) == "mid.proof"
    assert requests[0].url.params["appsecret_proof"] == expected
    assert requests[0].url.params["access_token"] == TOKEN


@pytest.mark.parametrize("page_id,token", [("", TOKEN), ("abc", TOKEN), ("12 34", TOKEN), (PAGE_ID, ""), (PAGE_ID, "  ")])
def test_invalid_init_fails_without_disclosure(page_id, token, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(MessengerSendError) as caught:
        HttpxMessengerBotSender(
            page_id=page_id,
            page_access_token=token,
            api_base_url=BASE_URL,
            api_version=API_VERSION,
        )
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "INVALID_TOKEN"
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "psid,text,code",
    [
        (PSID, "", "INVALID_TEXT"),
        (PSID, " ", "INVALID_TEXT"),
        (PSID, "x" * 2001, "INVALID_TEXT"),
        (PSID, "bad-\ud800", "INVALID_TEXT"),
        (PSID, {}, "INVALID_TEXT"),
        ("", TEXT, "INVALID_RECIPIENT"),
        ("bad-\ud800", TEXT, "INVALID_RECIPIENT"),
        (123, TEXT, "INVALID_RECIPIENT"),
    ],
)
def test_invalid_message_never_reaches_transport(psid, text, code):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"message_id": "bad"})

    with pytest.raises(MessengerSendError) as caught:
        asyncio.run(_sender(handle).send_text(psid, text))
    assert requests == []
    assert caught.value.code == code
    assert caught.value.retryable is False


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, RuntimeError])
def test_transport_exception_has_no_chain_or_log(error_type, caplog):
    caplog.set_level(logging.DEBUG)
    requests = []

    def handle(request):
        requests.append(request)
        raise error_type(f"{request.url} {PSID} {TEXT}")

    with pytest.raises(MessengerSendError) as caught:
        asyncio.run(_sender(handle).send_text(PSID, TEXT))
    _assert_private_failure(caught.value, caplog)
    assert caught.value.code == "SEND_UNAVAILABLE"
    assert caught.value.retryable is True
    assert len(requests) == 1


def _assert_private_failure(error, caplog):
    rendered = "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    for private in (TOKEN, PSID, TEXT):
        assert private not in str(error)
        assert private not in rendered
        assert private not in caplog.text


def test_cancellation_is_not_wrapped():
    def handle(request):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_sender(handle).send_text(PSID, TEXT))
