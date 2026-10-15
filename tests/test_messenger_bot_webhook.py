"""Messenger webhook ingress contracts. DB-free; fake store via dependency override."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.application.services import messenger_bot_dispatch
from api.infrastructure.adapters.postgres_messenger_events import MessengerStorageError
from api.interfaces.api import messenger_bot_routes

WEBHOOK_PATH = "/api/integrations/messenger-bot/webhook"
VERIFY_TOKEN = "verify-token-abc123"
APP_SECRET = "app-secret-xyz-0123456789abcdef0123456789"
_SIGNATURE_HEADER = "X-Hub-Signature-256"


def _msg(psid="PSID123", mid="m_mid.1", text="hello"):
    return {"sender": {"id": psid}, "message": {"mid": mid, "text": text}}


def _page_payload(items):
    return {"object": "page", "entry": [{"id": "PAGE1", "time": 1, "messaging": items}]}


def _sign(raw: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _post(client, payload, secret: str = APP_SECRET, tamper: bool = False):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = _sign(raw, secret)
    if tamper:
        raw = raw[:-1] + (b"X" if raw[-1:] != b"X" else b"Y")
    return client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={"content-type": "application/json", _SIGNATURE_HEADER: sig},
    )


class FakeEventStore:
    def __init__(self, fail_on: int | None = None, deterministic_on: int | None = None):
        self.calls: list[dict] = []
        self.fail_on = fail_on
        self.deterministic_on = deterministic_on

    async def __call__(self, **values):
        for row in self.calls:
            if row["event_ref"] == values["event_ref"]:
                return None
        if self.deterministic_on is not None and len(self.calls) + 1 == self.deterministic_on:
            raise MessengerStorageError("INVALID_EVENT")
        if self.fail_on is not None and len(self.calls) + 1 == self.fail_on:
            raise RuntimeError("db-down-marker")
        event_id = len(self.calls) + 1
        self.calls.append({"id": event_id, **values})
        return event_id


class RecordingScheduler:
    def __init__(self):
        self.scheduled: list[int] = []

    async def __call__(self, event_id: int):
        self.scheduled.append(event_id)


class _SettingsProxy:
    def __init__(self, *, enabled=True, verify_token=VERIFY_TOKEN,
                 app_secret=APP_SECRET, max_body_bytes=65536, page_id="PAGE1"):
        self.messenger_bot_enabled = enabled
        self.messenger_verify_token = SecretStr(verify_token)
        self.messenger_app_secret = SecretStr(app_secret)
        self.messenger_max_body_bytes = max_body_bytes
        self.messenger_page_id = page_id


async def _finish_tasks(cancel=False):
    tasks = tuple(messenger_bot_routes._background_tasks)
    if not tasks:
        return
    callbacks_done = asyncio.Event()
    remaining = set(tasks)

    def finished(task):
        remaining.discard(task)
        if not remaining:
            callbacks_done.set()

    for task in tasks:
        task.add_done_callback(finished)
        if cancel:
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
    await asyncio.wait_for(callbacks_done.wait(), timeout=5)


@pytest.fixture(autouse=True)
def isolated_seams(monkeypatch):
    original_scheduler = messenger_bot_dispatch.get_scheduler()
    original_settings = messenger_bot_routes.get_settings
    monkeypatch.setattr(messenger_bot_routes, "_background_tasks", set())
    try:
        yield
    finally:
        messenger_bot_dispatch.set_scheduler(original_scheduler)
        messenger_bot_routes.get_settings = original_settings
        assert not messenger_bot_routes._background_tasks, "Leaked ingress tasks"


@pytest.fixture
def store():
    return FakeEventStore()


@pytest.fixture
def scheduler():
    return RecordingScheduler()


@pytest.fixture
def client(monkeypatch, store, scheduler):
    settings = _SettingsProxy()
    monkeypatch.setattr(messenger_bot_routes, "get_settings", lambda: settings)
    messenger_bot_dispatch.set_scheduler(scheduler)
    app = FastAPI()
    app.include_router(messenger_bot_routes.router)
    app.dependency_overrides[messenger_bot_routes.get_messenger_event_store] = (
        lambda: store
    )
    with TestClient(app) as active_client:
        try:
            yield active_client
        finally:
            active_client.portal.call(_finish_tasks, True)
            app.dependency_overrides.clear()


def test_get_verify_correct_token_echoes_challenge(client):
    response = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.challenge": "CHALLENGE123",
                "hub.verify_token": VERIFY_TOKEN},
    )
    assert response.status_code == 200
    assert response.text == "CHALLENGE123"


def test_get_verify_wrong_token_403(client):
    response = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.challenge": "CHALLENGE123",
                "hub.verify_token": "wrong-token"},
    )
    assert response.status_code == 403
    assert response.json() == {"ok": False}


def test_get_verify_empty_configured_token_403(client, monkeypatch):
    monkeypatch.setattr(
        messenger_bot_routes, "get_settings",
        lambda: _SettingsProxy(verify_token=""),
    )
    response = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.challenge": "CHALLENGE123",
                "hub.verify_token": VERIFY_TOKEN},
    )
    assert response.status_code == 403


def test_post_valid_signature_accepted(client, store, scheduler):
    response = _post(client, _page_payload([_msg()]))
    client.portal.call(_finish_tasks)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}
    assert len(store.calls) == 1
    assert scheduler.scheduled == [1]


def test_post_tampered_byte_403(client, store):
    response = _post(client, _page_payload([_msg()]), tamper=True)
    assert response.status_code == 403
    assert store.calls == []


def test_post_two_messages_two_persists_distinct_keys(client, store, scheduler):
    payload = _page_payload([
        _msg(psid="PSID1", mid="m_mid.1", text="one"),
        _msg(psid="PSID2", mid="m_mid.2", text="two"),
    ])
    response = _post(client, payload)
    client.portal.call(_finish_tasks)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}
    assert [c["event_ref"] for c in store.calls] == ["m_mid.1", "m_mid.2"]
    assert scheduler.scheduled == [1, 2]


def test_post_persist_fail_second_event_503(client, scheduler):
    failing = FakeEventStore(fail_on=2)
    client.app.dependency_overrides[messenger_bot_routes.get_messenger_event_store] = (
        lambda: failing
    )
    payload = _page_payload([
        _msg(psid="PSID1", mid="m_mid.1", text="one"),
        _msg(psid="PSID2", mid="m_mid.2", text="two"),
    ])
    response = _post(client, payload)
    client.portal.call(_finish_tasks)
    assert response.status_code == 503
    assert response.json() == {"ok": False}


def test_post_wrong_page_entry_200_no_persist(client, store, scheduler, caplog):
    """A wrong-Page entry still answers 200 (Meta must not retry) but persists nothing.

    The batch is authenticated by the app secret, which is shared across every
    Page on the app, so the Page id is the only thing keeping another tenant's
    traffic out of this queue.
    """
    caplog.set_level(logging.WARNING)
    payload = _page_payload([_msg(psid="PSID-OTHER", mid="m_mid.other", text="other-page")])
    payload["entry"][0]["id"] = "PAGE2"
    response = _post(client, payload)
    client.portal.call(_finish_tasks)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": False}
    assert store.calls == []
    assert scheduler.scheduled == []
    assert "reason=wrong_page" in caplog.text
    assert "PSID-OTHER" not in caplog.text
    assert "other-page" not in caplog.text


def test_post_schema_incompatible_200_rejected(client, scheduler, caplog):
    """A deterministic persist fault rejects that event instead of 503-ing the batch.

    A 503 would make Meta redeliver a payload that can never be stored, looping
    forever and eventually costing the Page subscription. The sibling event still
    lands, and the reject log stays values-free.
    """
    caplog.set_level(logging.WARNING)
    failing = FakeEventStore(deterministic_on=2)
    client.app.dependency_overrides[messenger_bot_routes.get_messenger_event_store] = (
        lambda: failing
    )
    payload = _page_payload([
        _msg(psid="PSID1", mid="m_mid.1", text="one"),
        _msg(psid="PSID2", mid="m_mid.2", text="two"),
    ])
    response = _post(client, payload)
    client.portal.call(_finish_tasks)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}
    assert [c["event_ref"] for c in failing.calls] == ["m_mid.1"]
    assert scheduler.scheduled == [1]
    assert "reason=INVALID_EVENT" in caplog.text
    assert "PSID2" not in caplog.text
    assert "two" not in caplog.text
