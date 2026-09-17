"""Offline adapter boundary tests. Actual SQL/locking proof lives in PostgreSQL suite."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from api.infrastructure.adapters import postgres_zalo_bindings as bindings
from api.infrastructure.adapters import postgres_zalo_events as events
from tests.test_zalo_bot_bindings import FakeConnection as BindingConnection
from tests.test_zalo_bot_bindings import IDENTITY


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class IngressConnection:
    def __init__(self):
        self.tombstones = {}
        self.rows = {}
        self.sequence = 40
        self.fail_insert = False

    @asynccontextmanager
    async def transaction(self, **kwargs):
        old = deepcopy((self.tombstones, self.rows))
        try:
            yield
        except BaseException:
            self.tombstones, self.rows = old
            raise

    async def fetchval(self, sql, *args):
        self.sequence += 1
        return self.sequence

    async def fetchrow(self, sql, key, event_id, source):
        if key in self.tombstones:
            return None
        self.tombstones[key] = (event_id, source)
        return {"event_id": event_id}

    async def execute(self, sql, *args):
        if not args:
            return
        if self.fail_insert:
            raise RuntimeError("private-chat credential-dsn")
        self.rows[args[0]] = args


def install_ingress(monkeypatch):
    conn = IngressConnection()
    monkeypatch.setattr(events, "get_lead_pool", AsyncMock(return_value=FakePool(conn)))
    monkeypatch.setattr(events, "_validate_schema", AsyncMock())
    monkeypatch.setattr(events, "get_settings", lambda: SimpleNamespace(
        zalo_bot_enabled=True, zalo_bot_identity_secret=SecretStr("i" * 32)))
    return conn


@pytest.mark.asyncio
async def test_ingress_duplicate_survives_transport_deletion(monkeypatch):
    conn = install_ingress(monkeypatch)
    values = dict(chat_id="chat", message_id="message", event_name="message.text.received", text="query")
    assert await events.insert_event(**values) == 41
    conn.rows.clear()
    assert await events.insert_event(**values) is None
    assert conn.rows == {}
    assert list(conn.tombstones.values()) == [(41, "zalo-bot:event:v1:41")]


@pytest.mark.asyncio
async def test_ingress_failed_event_rolls_back_tombstone(monkeypatch):
    conn = install_ingress(monkeypatch)
    conn.fail_insert = True
    with pytest.raises(events.ZaloStorageError) as caught:
        await events.insert_event(chat_id="chat", message_id="message", event_name="text", text="query")
    assert conn.rows == {} and conn.tombstones == {}
    assert "private-chat" not in str(caught.value)
    assert "credential-dsn" not in str(caught.value)
    assert caught.value.__context__ is None
    conn.fail_insert = False
    assert await events.insert_event(chat_id="chat", message_id="message", event_name="text", text="query") == 42


@pytest.mark.asyncio
async def test_ownership_roundtrip_is_subtracted_before_return(monkeypatch):
    times = iter([10.0, 18.0])
    monkeypatch.setattr(events, "monotonic", lambda: next(times))
    conn = SimpleNamespace(fetchval=AsyncMock(return_value=110.0), execute=AsyncMock())

    @asynccontextmanager
    async def transaction():
        yield conn

    monkeypatch.setattr(events, "_transaction", transaction)
    store = events.PostgresZaloEventStore(settings=SimpleNamespace(zalo_bot_enabled=True))
    event = SimpleNamespace(id=1, identity_key=IDENTITY, claim_token=uuid4())
    assert await store.check_ownership(event, minimum_remaining=105) is None


@pytest.mark.asyncio
async def test_renewal_event_loss_rolls_back_actor_extension(monkeypatch):
    state = {"actor_until": 120}

    @asynccontextmanager
    async def transaction():
        before = state.copy()
        try:
            yield conn
        except BaseException:
            state.update(before)
            raise

    async def fetchrow(sql, *args):
        if "UPDATE zalo_bot_actor_leases" in sql:
            state["actor_until"] = 240
            return {"holder_token": args[1]}
        return None

    conn = SimpleNamespace(fetchrow=fetchrow)
    monkeypatch.setattr(events, "_transaction", transaction)
    store = events.PostgresZaloEventStore(settings=SimpleNamespace(zalo_bot_enabled=True, zalo_bot_lease_seconds=120))
    event = SimpleNamespace(id=1, identity_key=IDENTITY, claim_token=uuid4())
    assert await store.renew_lease(event) is None
    assert state["actor_until"] == 120


@pytest.mark.asyncio
async def test_store_authorization_rechecks_current_sales_and_binding(monkeypatch):
    conn = BindingConnection()
    conn.bindings[IDENTITY] = {"sales_id": 7, "is_active": True}
    monkeypatch.setattr(bindings, "get_lead_pool", AsyncMock(return_value=FakePool(conn)))
    store = events.PostgresZaloEventStore(settings=SimpleNamespace(zalo_bot_enabled=True))
    assert await store.authorize(IDENTITY) == 7
    conn.sales[7] = False
    assert await store.authorize(IDENTITY) is None
    conn.sales[7] = True
    conn.bindings[IDENTITY]["is_active"] = False
    assert await store.authorize(IDENTITY) is None
    conn.error = RuntimeError("missing schema")
    with pytest.raises(RuntimeError):
        await store.authorize(IDENTITY)


@pytest.mark.parametrize("outcome", ["invalid-json", "[]", [], {"answer": "cached"}])
def test_corrupt_cache_never_becomes_uncached(outcome):
    with pytest.raises(events.ZaloStorageError):
        events._decode_outcome(outcome)
