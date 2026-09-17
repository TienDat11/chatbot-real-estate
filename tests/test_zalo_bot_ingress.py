"""ZB-01 ingress contracts. DB-free; migration assertions are SQL shape only."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.application.services import zalo_bot_dispatch
from api.interfaces.api import zalo_bot_routes

WEBHOOK_PATH = "/api/integrations/zalo-bot/webhook"
VALID_SECRET = "test-secret-12345"
_SECRET_MARKER = "chat-text-must-not-be-logged-9f3a7b2c"


def _private_text_event(text="hello world", chat_id="chat-123", message_id="msg-456",
                        chat_type="PRIVATE", event_name="message.text.received", ok=True):
    return {"ok": ok, "result": {"event_name": event_name, "message": {
        "message_id": message_id, "text": text,
        "chat": {"id": chat_id, "chat_type": chat_type},
    }, "from": {"id": "from-789"}}}


class FakeEventStore:
    def __init__(self):
        self.inserted = []

    async def __call__(self, **values):
        if any((row["chat_id"], row["message_id"]) ==
               (values["chat_id"], values["message_id"]) for row in self.inserted):
            return None
        event_id = len(self.inserted) + 1
        self.inserted.append({"id": event_id, "status": "pending", **values})
        return event_id


class RecordingScheduler:
    def __init__(self):
        self.scheduled = []

    async def __call__(self, event_id):
        self.scheduled.append(event_id)


class _SettingsProxy:
    def __init__(self, *, zalo_bot_enabled=True, zalo_bot_webhook_secret=VALID_SECRET,
                 zalo_bot_max_body_bytes=65536):
        self.zalo_bot_enabled = zalo_bot_enabled
        self.zalo_bot_webhook_secret = SecretStr(zalo_bot_webhook_secret)
        self.zalo_bot_max_body_bytes = zalo_bot_max_body_bytes


async def _finish_tasks(cancel=False):
    tasks = tuple(zalo_bot_routes._background_tasks)
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
    original_scheduler = zalo_bot_dispatch.get_scheduler()
    original_settings = zalo_bot_routes.get_settings
    monkeypatch.setattr(zalo_bot_routes, "_background_tasks", set())
    try:
        yield
    finally:
        zalo_bot_dispatch.set_scheduler(original_scheduler)
        zalo_bot_routes.get_settings = original_settings
        assert not zalo_bot_routes._background_tasks, "Leaked ingress tasks"


@pytest.fixture
def store():
    return FakeEventStore()


@pytest.fixture
def scheduler():
    return RecordingScheduler()


@pytest.fixture
def client(monkeypatch, store, scheduler):
    settings = _SettingsProxy()
    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: settings)
    zalo_bot_dispatch.set_scheduler(scheduler)
    app = FastAPI()
    app.include_router(zalo_bot_routes.router)
    app.state.settings = settings
    app.dependency_overrides[zalo_bot_routes.get_zalo_event_store] = lambda: store
    with TestClient(app) as active_client:
        try:
            yield active_client
        finally:
            active_client.portal.call(_finish_tasks, True)
            app.dependency_overrides.clear()


@pytest.mark.parametrize("headers", [{}, {"X-Bot-Api-Secret-Token": "wrong-secret"},
                                      {b"X-Bot-Api-Secret-Token": b"\xe9"}])
def test_missing_wrong_and_non_ascii_secret_fail_closed(client, store, scheduler, headers):
    response = client.post(WEBHOOK_PATH, json=_private_text_event(), headers=headers)
    assert response.status_code == 403
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_valid_private_text_persisted_once_and_scheduled_once(client, store, scheduler):
    payload = _private_text_event(text="Căn 2PN đẹp", chat_id="chat-xyz", message_id="msg-abc")
    headers = {"X-Bot-Api-Secret-Token": VALID_SECRET}
    first = client.post(WEBHOOK_PATH, json=payload, headers=headers)
    duplicate = client.post(WEBHOOK_PATH, json=payload, headers=headers)
    client.portal.call(_finish_tasks)
    assert first.status_code == duplicate.status_code == 200
    assert first.json() == {"ok": True, "accepted": True}
    assert duplicate.json() == {"ok": True, "accepted": False}
    assert store.inserted == [{"id": 1, "status": "pending", "chat_id": "chat-xyz",
                               "message_id": "msg-abc", "text": "Căn 2PN đẹp",
                               "event_name": "message.text.received"}]
    assert scheduler.scheduled == [1]


@pytest.mark.parametrize("field", ["text", "chat_id", "message_id"])
@pytest.mark.parametrize("value", [123, ""])
def test_invalid_projected_field_zero_work(client, store, scheduler, field, value):
    payload = _private_text_event(**{field: value})
    response = client.post(WEBHOOK_PATH, json=payload, headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": False}
    assert store.inserted == []
    assert scheduler.scheduled == []


@pytest.mark.parametrize("values", [{"chat_type": "GROUP"}, {"event_name": "message.image.received"}])
def test_unsupported_event_zero_work(client, store, scheduler, values):
    response = client.post(WEBHOOK_PATH, json=_private_text_event(**values),
                           headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": False}
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_disabled_valid_event_zero_work(client, store, scheduler):
    client.app.state.settings.zalo_bot_enabled = False
    response = client.post(WEBHOOK_PATH, json=_private_text_event(),
                           headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": False}
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_empty_configured_secret_fail_closed(client, store, scheduler):
    client.app.state.settings.zalo_bot_webhook_secret = SecretStr("")
    response = client.post(WEBHOOK_PATH, json=_private_text_event(),
                           headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
    assert response.status_code == 403
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_no_provider_outbound_connection(client, scheduler, monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Outbound network connection attempted")

    with monkeypatch.context() as guard:
        guard.setattr(socket.socket, "connect", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        response = client.post(WEBHOOK_PATH, json=_private_text_event(),
                               headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
        client.portal.call(_finish_tasks)
    assert response.json() == {"ok": True, "accepted": True}
    assert scheduler.scheduled == [1]
    assert attempts == []


def test_ack_returns_while_scheduler_is_blocked(client, store):
    release = asyncio.Event()
    started = threading.Event()
    completed = []

    async def blocked(event_id):
        started.set()
        await release.wait()
        completed.append(event_id)

    zalo_bot_dispatch.set_scheduler(blocked)
    try:
        response = client.post(WEBHOOK_PATH, json=_private_text_event(),
                               headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
        assert started.wait(5)
        assert response.status_code == 200
        assert response.json() == {"ok": True, "accepted": True}
        assert [row["status"] for row in store.inserted] == ["pending"]
        assert completed == []
    finally:
        client.portal.call(release.set)
        client.portal.call(_finish_tasks)
    assert completed == [1]


@pytest.mark.parametrize("declared", [None, "1"])
def test_actual_chunked_body_cap_stops_receiving(monkeypatch, store, scheduler, declared):
    from starlette.requests import Request

    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: _SettingsProxy(zalo_bot_max_body_bytes=6))
    zalo_bot_dispatch.set_scheduler(scheduler)
    headers = [(b"x-bot-api-secret-token", VALID_SECRET.encode())]
    if declared is not None:
        headers.append((b"content-length", declared.encode()))
    received = []

    async def receive():
        received.append(True)
        return {"type": "http.request", "body": b"12345", "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": WEBHOOK_PATH, "headers": headers}, receive)
    response = asyncio.run(zalo_bot_routes.zalo_webhook(request, store))
    assert response.status_code == 413
    assert len(received) == 2
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_migration_new_table_sql_shape_only():
    """Static shape assertion; not live PostgreSQL migration verification."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db/migrations/2026-09-17-zalo-bot-events.sql").read_text(encoding="utf-8")
    normalized = " ".join(sql.lower().split())
    assert "if to_regclass('public.zalo_bot_events') is null then create table" in normalized
    assert "id bigserial primary key" in normalized
    for column in ("chat_id", "message_id", "event_name", "text"):
        assert f"{column} text not null" in normalized
    assert "status text not null default 'pending'" in normalized
    for column in ("received_at", "updated_at"):
        assert f"{column} timestamptz not null default now()" in normalized
    assert "unique (chat_id, message_id)" in normalized
    assert "on zalo_bot_events (status, received_at)" in normalized
    assert "begin;" in normalized and normalized.endswith("commit;")


@pytest.mark.parametrize("payload", [
    {"ok": True, **_private_text_event()["result"]},
    {"ok": True},
    {"ok": True, "result": []},
    _private_text_event(ok=False),
    {"ok": 1, "result": _private_text_event()["result"]},
    [],
])
def test_strict_envelope_ignores_unsupported_payload(client, store, scheduler, payload):
    response = client.post(
        WEBHOOK_PATH, json=payload,
        headers={"X-Bot-Api-Secret-Token": VALID_SECRET},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": False}
    assert store.inserted == []
    assert scheduler.scheduled == []


@pytest.mark.parametrize("enabled,length,valid", [
    (False, 0, True), (True, 0, False), (True, 7, False),
    (True, 8, True), (True, 256, True), (True, 257, False),
])
def test_settings_webhook_secret_boundary(enabled, length, valid):
    from pydantic import ValidationError

    from api.infrastructure.config.config import Settings

    secret = "x" * length
    values = dict(
        _env_file=None, app_env="test", enforce_approved_llm_provider=False,
        zalo_bot_enabled=enabled, zalo_bot_webhook_secret=secret,
    )
    if valid:
        settings = Settings(**values)
        assert settings.zalo_bot_enabled is enabled
        assert settings.zalo_bot_webhook_secret.get_secret_value() == secret
    else:
        with pytest.raises(ValidationError) as error:
            Settings(**values)
        if secret:
            assert secret not in str(error.value)


@pytest.mark.parametrize("cap", [0, -1])
def test_settings_reject_nonpositive_body_cap(cap):
    from pydantic import ValidationError

    from api.infrastructure.config.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="test", enforce_approved_llm_provider=False,
                 zalo_bot_enabled=False, zalo_bot_max_body_bytes=cap)


@pytest.mark.parametrize("length,status_code", [("-1", 400), ("invalid", 400), ("65537", 413)])
def test_declared_length_rejected_before_body_read(client, store, scheduler, monkeypatch, length, status_code):
    async def forbidden_read(*args):
        pytest.fail("Rejected declaration must not read body")

    monkeypatch.setattr(zalo_bot_routes, "_recv_bounded_body", forbidden_read)
    response = client.post(WEBHOOK_PATH, content=b"{}", headers={
        "X-Bot-Api-Secret-Token": VALID_SECRET, "Content-Length": length,
    })
    assert response.status_code == status_code
    assert store.inserted == []
    assert scheduler.scheduled == []


@pytest.mark.parametrize("body", [
    pytest.param(b"not valid json {{{", id="malformed-json"),
    pytest.param(b"\xff", id="invalid-encoding"),
    pytest.param(b"9" * 5000, id="oversized-integer"),
])
def test_unparseable_json_never_returns_500(client, store, scheduler, body):
    response = client.post(WEBHOOK_PATH, content=body, headers={
        "X-Bot-Api-Secret-Token": VALID_SECRET,
    })
    assert response.status_code == 400
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_deep_json_never_returns_500_or_creates_work(client, store, scheduler):
    response = client.post(WEBHOOK_PATH, content=b"[" * 2000 + b"]" * 2000, headers={
        "X-Bot-Api-Secret-Token": VALID_SECRET,
    })
    # Parser recursion limits differ by Python version: reject safely or ignore
    # the parsed non-event. Neither outcome may persist or schedule work.
    assert response.status_code in (400, 200)
    if response.status_code == 200:
        assert response.json() == {"ok": True, "accepted": False}
    assert store.inserted == []
    assert scheduler.scheduled == []


@pytest.mark.parametrize("content_type", [None, "application/json", "Application/JSON; charset=utf-8"])
def test_json_content_types_accepted(client, content_type):
    headers = {"X-Bot-Api-Secret-Token": VALID_SECRET}
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.post(WEBHOOK_PATH, content=json.dumps(_private_text_event()), headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}


@pytest.mark.parametrize("secret,status_code", [(VALID_SECRET, 415), ("wrong-secret", 403)])
def test_incompatible_content_type_auth_first(client, store, scheduler, monkeypatch, secret, status_code):
    async def forbidden_read(*args):
        pytest.fail("Rejected media type/auth must not read body")

    monkeypatch.setattr(zalo_bot_routes, "_recv_bounded_body", forbidden_read)
    response = client.post(WEBHOOK_PATH, content=b"invalid json", headers={
        "X-Bot-Api-Secret-Token": secret, "Content-Type": "text/plain",
    })
    assert response.status_code == status_code
    assert store.inserted == []
    assert scheduler.scheduled == []


def test_projector_returns_frozen_event_without_mutating_payload():
    from dataclasses import FrozenInstanceError

    from api.application.services.zalo_bot_projection import (
        ZaloInboundTextEvent,
        project_zalo_private_text_event,
    )

    payload = _private_text_event(text=" Căn 2PN đẹp ")
    before = json.dumps(payload)
    projected = project_zalo_private_text_event(payload)
    assert projected == ZaloInboundTextEvent("chat-123", "msg-456", " Căn 2PN đẹp ", "message.text.received")
    assert json.dumps(payload) == before
    with pytest.raises(FrozenInstanceError):
        projected.text = "changed"


@pytest.mark.parametrize("path,value", [
    ((), None), ((), []), (("ok",), 1), (("result",), None),
    (("result", "message"), []), (("result", "message", "chat"), "chat"),
    (("result", "event_name"), "message.image.received"),
    (("result", "message", "chat", "chat_type"), "GROUP"),
    (("result", "message", "chat", "id"), ""),
    (("result", "message", "chat", "id"), 12),
    (("result", "message", "message_id"), ""),
    (("result", "message", "message_id"), []),
    (("result", "message", "text"), ""),
    (("result", "message", "text"), 12),
])
def test_projector_rejects_unsupported_shapes(path, value):
    from api.application.services.zalo_bot_projection import project_zalo_private_text_event

    payload = _private_text_event()
    if path:
        container = payload
        for key in path[:-1]:
            container = container[key]
        container[path[-1]] = value
    else:
        payload = value
    assert project_zalo_private_text_event(payload) is None


@pytest.mark.parametrize("extra,accepted", [(0, True), (1, False)])
def test_route_query_length_boundary(client, extra, accepted):
    from api.domain.value_objects.constants import MAX_QUERY_LENGTH

    response = client.post(WEBHOOK_PATH, json=_private_text_event(text="x" * (MAX_QUERY_LENGTH + extra)),
                           headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": accepted}


def test_adapter_atomic_insert_and_conflict(monkeypatch):
    from contextlib import asynccontextmanager

    from api.infrastructure.adapters import postgres_zalo_events

    queries = []

    class Connection:
        async def fetchrow(self, sql, *params):
            queries.append((" ".join(sql.split()), params))
            return {"id": 41} if len(queries) == 1 else None

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async def shared_pool():
        return Pool()

    monkeypatch.setattr(postgres_zalo_events, "get_lead_pool", shared_pool)

    async def insert_twice():
        values = dict(chat_id="chat", message_id="message", event_name="message.text.received", text="text")
        assert await postgres_zalo_events.insert_event(**values) == 41
        assert await postgres_zalo_events.insert_event(**values) is None

    asyncio.run(insert_twice())
    assert len(queries) == 2
    for sql, params in queries:
        assert "INSERT INTO zalo_bot_events (chat_id, message_id, event_name, text) VALUES" in sql
        assert "ON CONFLICT (chat_id, message_id) DO NOTHING RETURNING id" in sql
        assert "VALUES ($1, $2, $3, $4)" in sql
        assert params == ("chat", "message", "message.text.received", "text")


def test_migration_existing_schema_sql_shape_only():
    """Static SQL contract, not evidence of execution against live PostgreSQL."""
    import re
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db/migrations/2026-09-17-zalo-bot-events.sql").read_text(encoding="utf-8")
    sql = " ".join(re.sub(r"--[^\n]*", "", sql).lower().split())
    assert "attname = 'provider_message_id'" in sql
    assert "and not exists ( select 1 from pg_attribute" in sql
    assert "attname = 'message_id'" in sql
    assert "rename column provider_message_id to message_id" in sql
    assert "i.indisunique and i.indisvalid and i.indimmediate" in sql
    assert "i.indpred is null and i.indexprs is null" in sql
    assert "i.indnkeyatts = 2" in sql
    assert "array['chat_id', 'message_id']::text[]" in sql
    assert not re.search(r"\b(drop|delete|truncate|update)\b", sql)


def test_spawn_retains_task_until_done_callback():
    async def scenario():
        release = asyncio.Event()
        callback_finished = asyncio.Event()
        task = zalo_bot_routes._spawn(release.wait())
        task.add_done_callback(lambda _: callback_finished.set())
        assert task in zalo_bot_routes._background_tasks
        release.set()
        await asyncio.wait_for(callback_finished.wait(), timeout=5)
        assert task not in zalo_bot_routes._background_tasks
        assert task.result() is True

    asyncio.run(scenario())


def test_dispatch_setup_failure_still_accepts_pending_row(client, store, monkeypatch, caplog):
    import inspect

    attempted = []

    def unavailable(coro):
        attempted.append(coro)
        raise RuntimeError(_SECRET_MARKER)

    # Patch only while the route is setting up its task, not TestClient's loop.
    original_spawn = zalo_bot_routes._spawn

    def failed_spawn(coro):
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_task", unavailable)
            return original_spawn(coro)

    monkeypatch.setattr(zalo_bot_routes, "_spawn", failed_spawn)
    response = client.post(WEBHOOK_PATH, json=_private_text_event(), headers={
        "X-Bot-Api-Secret-Token": VALID_SECRET,
    })
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}
    assert [row["status"] for row in store.inserted] == ["pending"]
    assert len(attempted) == 1
    assert inspect.getcoroutinestate(attempted[0]) == inspect.CORO_CLOSED
    assert _SECRET_MARKER not in caplog.text


@pytest.mark.parametrize("failure", [None, "store", "scheduler", "setup"])
def test_all_logs_exclude_inbound_identifiers_text_and_secret(client, store, scheduler, monkeypatch, caplog, failure):
    markers = ("chat-private-f6e321", "message-private-91d376", "text-private-763ea2", "secret-private-a514c9")
    chat_id, message_id, text, secret = markers
    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: _SettingsProxy(zalo_bot_webhook_secret=secret))
    done = threading.Event()
    original_callback = zalo_bot_routes._log_task_exception

    def callback(task):
        original_callback(task)
        done.set()

    async def fail(*args, **kwargs):
        raise RuntimeError(" ".join(markers))

    monkeypatch.setattr(zalo_bot_routes, "_log_task_exception", callback)
    if failure == "store":
        client.app.dependency_overrides[zalo_bot_routes.get_zalo_event_store] = lambda: fail
    elif failure == "scheduler":
        zalo_bot_dispatch.set_scheduler(fail)
    elif failure == "setup":
        def fail_setup(coro):
            coro.close()
            raise RuntimeError(" ".join(markers))
        monkeypatch.setattr(zalo_bot_routes, "_spawn", fail_setup)
    with caplog.at_level(logging.DEBUG):
        response = client.post(WEBHOOK_PATH, json=_private_text_event(text=text, chat_id=chat_id, message_id=message_id),
                               headers={"X-Bot-Api-Secret-Token": secret})
        if failure in (None, "scheduler"):
            assert done.wait(5), "Dispatch callback did not complete"
    assert response.status_code == (503 if failure == "store" else 200)
    if failure == "store":
        assert response.json() == {"ok": False}
        assert store.inserted == []
        assert scheduler.scheduled == []
    if failure != "store":
        assert response.json() == {"ok": True, "accepted": True}
        assert [row["status"] for row in store.inserted] == ["pending"]
    for marker in markers:
        assert marker not in caplog.text
        assert all(marker not in record.getMessage() for record in caplog.records)
    assert all(record.exc_info is None for record in caplog.records)
    if failure:
        assert any("RuntimeError" in record.getMessage() for record in caplog.records)


def test_real_create_app_keeps_webhook_and_existing_routes(monkeypatch, store, scheduler):
    from contextlib import asynccontextmanager

    from fastapi.routing import iter_route_contexts

    from api.infrastructure.config import config
    from api.interfaces.api import assistant_routes, crm_routes, main, sessions
    from api.interfaces.api.deps import require_training_sales

    @asynccontextmanager
    async def no_external_startup(app):
        yield

    # Exercise the real factory/router/CORS assembly, not infrastructure startup.
    monkeypatch.setattr(main, "_lifespan", no_external_startup)
    monkeypatch.setattr(config, "export_runtime_env", lambda: None)
    original_get_cfg = main.get_cfg
    monkeypatch.setattr(main, "get_cfg", lambda key: ["https://allowed.example"] if key == "cors_origins" else original_get_cfg(key))
    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: _SettingsProxy())
    zalo_bot_dispatch.set_scheduler(scheduler)
    app = main.create_app()
    app.dependency_overrides[zalo_bot_routes.get_zalo_event_store] = lambda: store

    def endpoint(path, method):
        matches = [route for route in iter_route_contexts(app.routes) if route.path == path
                   and method in (route.methods or set())]
        assert len(matches) == 1
        return matches[0]

    assert endpoint(WEBHOOK_PATH, "POST").endpoint is zalo_bot_routes.zalo_webhook
    assert endpoint("/api/assistant/query", "POST").endpoint is assistant_routes.assistant_query_json
    assert endpoint("/api/assistant/query/stream", "POST").endpoint is assistant_routes.assistant_query_stream
    assert endpoint("/api/sessions", "GET").endpoint is sessions.list_sessions
    assert endpoint("/api/sessions/{session_id}/messages", "GET").endpoint is sessions.list_messages
    assert endpoint("/api/crm/customers/search", "POST").endpoint is crm_routes.search_customer_by_phone
    legacy = endpoint("/query", "POST")
    assert require_training_sales in [dependency.call for dependency in legacy.dependant.dependencies]

    with TestClient(app) as active_client:
        try:
            response = active_client.post(WEBHOOK_PATH, json=_private_text_event(),
                                          headers={"X-Bot-Api-Secret-Token": VALID_SECRET})
            active_client.portal.call(_finish_tasks)
            assert response.json() == {"ok": True, "accepted": True}
            assert scheduler.scheduled == [1]
            preflight = active_client.options("/query", headers={
                "Origin": "https://allowed.example", "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Device-Id",
            })
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == "https://allowed.example"
            rejected = active_client.options("/query", headers={
                "Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST",
            })
            assert rejected.status_code == 400
            assert "access-control-allow-origin" not in rejected.headers
        finally:
            active_client.portal.call(_finish_tasks, True)
            app.dependency_overrides.clear()


@pytest.mark.parametrize("secret", ["pässwörd-1234567", "\U0001f512" * 8, "密钥测试安全配置"],
                         ids=["latin", "emoji", "cjk"])
def test_unicode_secret_matches_exact_utf8_header(client, store, scheduler, monkeypatch, secret):
    from api.infrastructure.config.config import Settings

    settings = Settings(_env_file=None, app_env="test", enforce_approved_llm_provider=False,
                        zalo_bot_enabled=True, zalo_bot_webhook_secret=secret)
    monkeypatch.setattr(zalo_bot_routes, "get_settings", lambda: settings)
    wrong = client.post(WEBHOOK_PATH, json=_private_text_event(), headers={
        b"X-Bot-Api-Secret-Token": (secret + "錯").encode("utf-8"),
    })
    assert wrong.status_code == 403
    assert store.inserted == []
    assert scheduler.scheduled == []
    response = client.post(WEBHOOK_PATH, json=_private_text_event(), headers={
        b"X-Bot-Api-Secret-Token": secret.encode("utf-8"),
    })
    client.portal.call(_finish_tasks)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": True}
    assert len(store.inserted) == 1
    assert scheduler.scheduled == [1]


@pytest.mark.parametrize("secret,markers", [
    ("Q7!", ("Q7!",)),
    (["Q7!"], ("Q7!",)),
    (739182640517, ("739182640517",)),
    ({"value": "Q7!"}, ("Q7!",)),
    (True, ("True", "true")),
], ids=["string", "list", "int", "dict", "bool"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["raw", "SecretStr"])
def test_invalid_secret_safe_on_all_validation_surfaces(secret, markers, wrapped):
    import traceback

    from pydantic import ValidationError

    from api.infrastructure.config.config import Settings

    values = dict(_env_file=None, app_env="test", enforce_approved_llm_provider=False,
                  zalo_bot_enabled=isinstance(secret, str),
                  zalo_bot_webhook_secret=SecretStr(secret) if wrapped else secret)
    with pytest.raises(ValidationError) as caught:
        Settings(**values)
    exc = caught.value
    surfaces = {
        "str": str(exc), "repr": repr(exc), "json": exc.json(),
        "errors": repr(exc.errors()), "errors_json": json.dumps(exc.errors(), default=str),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    if isinstance(secret, str):
        surfaces["settings_repr"] = repr(Settings(**{**values, "zalo_bot_enabled": False}))
    leaked = {
        name for name, rendered in surfaces.items()
        if any(marker in rendered for marker in markers)
    }
    assert leaked == set()
    assert values["zalo_bot_webhook_secret"] == (SecretStr(secret) if wrapped else secret)


@pytest.mark.parametrize("mapping_type", ["UserDict", "MappingProxyType", "custom"],
                         ids=["UserDict", "MappingProxyType", "custom-Mapping"])
@pytest.mark.parametrize("secret", [["Q7!"], 991, {"value": "Q7!"}],
                         ids=["list", "int", "dict"])
def test_non_dict_mapping_secret_safe_on_all_surfaces(mapping_type, secret):
    import traceback
    from collections import UserDict
    from collections.abc import Mapping
    from types import MappingProxyType

    from pydantic import ValidationError

    from api.infrastructure.config.config import Settings

    class _CustomMapping(Mapping):
        def __init__(self, data):
            self._data = dict(data)

        def __getitem__(self, key):
            return self._data[key]

        def __iter__(self):
            return iter(self._data)

        def __len__(self):
            return len(self._data)

    base = {"app_env": "test", "enforce_approved_llm_provider": False,
            "zalo_bot_enabled": False, "zalo_bot_webhook_secret": secret}
    mapping = {"UserDict": UserDict, "MappingProxyType": MappingProxyType,
               "custom": _CustomMapping}[mapping_type](base)
    assert isinstance(mapping, Mapping) and not isinstance(mapping, dict)
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate(mapping, strict=True)
    exc = caught.value
    surfaces = {
        "str": str(exc), "repr": repr(exc), "json": exc.json(),
        "errors": repr(exc.errors()), "errors_json": json.dumps(exc.errors(), default=str),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    leaked = {name for name, rendered in surfaces.items() if "Q7!" in rendered or "991" in rendered}
    assert leaked == set()


@pytest.mark.parametrize("mapping_type", ["UserDict", "MappingProxyType", "custom"],
                         ids=["UserDict", "MappingProxyType", "custom-Mapping"])
def test_valid_mapping_still_loads(mapping_type):
    from collections import UserDict
    from collections.abc import Mapping
    from types import MappingProxyType

    from api.infrastructure.config.config import Settings

    class _CustomMapping(Mapping):
        def __init__(self, data):
            self._data = dict(data)

        def __getitem__(self, key):
            return self._data[key]

        def __iter__(self):
            return iter(self._data)

        def __len__(self):
            return len(self._data)

    secret = "valid-mapping-secret-01"
    base = {"app_env": "test", "enforce_approved_llm_provider": False,
            "zalo_bot_enabled": False, "zalo_bot_webhook_secret": secret}
    mapping = {"UserDict": UserDict, "MappingProxyType": MappingProxyType,
               "custom": _CustomMapping}[mapping_type](base)
    assert isinstance(mapping, Mapping) and not isinstance(mapping, dict)
    settings = Settings.model_validate(mapping, strict=True)
    assert settings.app_env == "test"
    assert settings.zalo_bot_webhook_secret.get_secret_value() == secret
    assert secret not in repr(settings)
    assert secret not in repr(settings.model_dump())
    assert "**********" in repr(settings.zalo_bot_webhook_secret)


@pytest.mark.parametrize("location", ["top", "nested"])
def test_duplicate_json_keys_rejected_without_work(client, store, scheduler, monkeypatch, location):
    attempts = []

    def forbidden_connect(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Outbound connection forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)
    body = json.dumps(_private_text_event())
    if location == "top":
        body = body.replace('"ok": true', '"ok": false, "ok": true', 1)
    else:
        body = body.replace('"text": "hello world"', '"text": "ignored", "text": "hello world"', 1)
    response = client.post(WEBHOOK_PATH, content=body, headers={
        "X-Bot-Api-Secret-Token": VALID_SECRET, "Content-Type": "application/json",
    })
    client.portal.call(_finish_tasks)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "accepted": False}
    assert store.inserted == []
    assert scheduler.scheduled == []
    assert attempts == []
