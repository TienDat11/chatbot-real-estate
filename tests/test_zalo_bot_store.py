"""Offline persistence, JSONB and migration checks; no database execution."""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from api.infrastructure.adapters import postgres_zalo_bindings as bindings
from api.infrastructure.adapters import postgres_zalo_events as events
from tests.migration_sql_support import split_statements
from tests.test_zalo_bot_bindings import FakeConnection as BindingConnection
from tests.test_zalo_bot_bindings import IDENTITY


class FakeConnection:
    def __init__(self):
        self.rows = {}
        self.error = None

    async def fetchrow(self, query, *args, timeout=None):
        if self.error:
            raise self.error
        if query.lstrip().startswith("INSERT"):
            for row in self.rows.values():
                if (row["chat_id"], row["message_id"]) == args[:2]:
                    return None
            event_id = len(self.rows) + 1
            self.rows[event_id] = dict(
                id=event_id, chat_id=args[0], message_id=args[1], event_name=args[2],
                text=args[3], status="pending", identity_key=None,
                source_event_key=None, assistant_outcome=None, outbound_message_id=None,
                completed_at=None,
            )
            return {"id": event_id}
        assert timeout > 0 and "WHERE id = $1" in query
        row = self.rows.get(args[0])
        if row is None:
            return None
        if query.lstrip().startswith("SELECT"):
            return {key: value for key, value in row.items()
                    if key not in {"message_id", "event_name", "completed_at"}}
        if "SET identity_key = $2, source_event_key = $3" in query:
            row.update(identity_key=args[1], source_event_key=args[2])
        elif "SET assistant_outcome = $2::jsonb" in query:
            # asyncpg default codec returns JSONB as str, not dict.
            json.loads(args[1])
            row["assistant_outcome"] = args[1]
        elif "SET outbound_message_id = $2" in query:
            row["outbound_message_id"] = args[1]
        elif "SET status = $2" in query:
            assert "CASE WHEN $2 = 'done' THEN now() ELSE NULL END" in query
            row.update(status=args[1], completed_at="timestamp" if args[1] == "done" else None)
        else:
            raise AssertionError(f"Unexpected query: {query}")
        return {"id": row["id"]}


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
def connection(monkeypatch):
    conn = FakeConnection()

    async def get_pool():
        return FakePool(conn)

    monkeypatch.setattr(events, "get_lead_pool", get_pool)
    return conn


async def insert():
    return await events.insert_event(
        chat_id="chat", message_id="message", event_name="message.text.received", text="query"
    )


@pytest.mark.asyncio
async def test_ingress_dedupe_and_roundtrip_survive_new_store_instance(connection):
    event_id = await insert()
    assert await insert() is None
    store = events.PostgresZaloEventStore()
    await store.set_identity(event_id, IDENTITY, f"zalo-bot:event:v1:{event_id}")
    outcome = {"answer": "Tiếng Việt", "resolution_status": "resolved", "project_key": "project"}
    await store.cache_outcome(event_id, outcome)
    # Cache durable before provider id; normal retry must find this pending.
    cached = await events.PostgresZaloEventStore().get_event(event_id)
    assert cached.assistant_outcome == outcome
    assert cached.status == "pending" and cached.outbound_message_id is None
    assert cached.identity_key == IDENTITY
    assert cached.source_event_key == f"zalo-bot:event:v1:{event_id}"
    await store.record_outbound(event_id, "provider-id")
    sent = await events.PostgresZaloEventStore().get_event(event_id)
    assert sent.outbound_message_id == "provider-id" and sent.status == "pending"
    await store.set_status(event_id, "done")
    done = await events.PostgresZaloEventStore().get_event(event_id)
    assert done.status == "done" and done.assistant_outcome == outcome
    assert connection.rows[event_id]["completed_at"] is not None


@pytest.mark.asyncio
async def test_rejection_is_terminal_without_completion_timestamp(connection):
    event_id = await insert()
    store = events.PostgresZaloEventStore()
    await store.set_status(event_id, "rejected")
    assert (await store.get_event(event_id)).status == "rejected"
    assert connection.rows[event_id]["completed_at"] is None
    with pytest.raises(ValueError):
        await store.set_status(event_id, "processing")
    assert (await store.get_event(event_id)).status == "rejected"


@pytest.mark.asyncio
async def test_absent_rows_and_outages_do_not_masquerade_as_success(connection):
    store = events.PostgresZaloEventStore()
    assert await store.get_event(99) is None
    with pytest.raises(RuntimeError):
        await store.cache_outcome(99, {"answer": "missing"})
    connection.error = RuntimeError("raw-chat secret-dsn")
    with pytest.raises(RuntimeError) as caught:
        await store.get_event(99)
    assert "raw-chat" not in str(caught.value)
    assert "secret-dsn" not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ['{"answer":"cached"}', {"answer": "cached"}])
async def test_jsonb_default_and_custom_decoders_preserve_cache(connection, outcome):
    event_id = await insert()
    connection.rows[event_id]["assistant_outcome"] = outcome
    event = await events.PostgresZaloEventStore().get_event(event_id)
    assert event.assistant_outcome == {"answer": "cached"}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["invalid-json", "[]", []])
async def test_corrupt_cache_is_not_silently_treated_as_uncached(connection, outcome):
    event_id = await insert()
    connection.rows[event_id]["assistant_outcome"] = outcome
    with pytest.raises(RuntimeError):
        await events.PostgresZaloEventStore().get_event(event_id)
    assert connection.rows[event_id]["status"] == "pending"


@pytest.mark.asyncio
async def test_store_authorization_rechecks_current_sales_and_binding(monkeypatch):
    conn = BindingConnection()
    conn.bindings[IDENTITY] = {"sales_id": 7, "is_active": True}

    async def get_pool():
        return FakePool(conn)

    monkeypatch.setattr(bindings, "get_lead_pool", get_pool)
    store = events.PostgresZaloEventStore()
    assert await store.authorize(IDENTITY) == 7
    conn.sales[7] = False
    assert await store.authorize(IDENTITY) is None
    conn.sales[7] = True
    conn.bindings[IDENTITY]["is_active"] = False
    assert await store.authorize(IDENTITY) is None
    conn.error = RuntimeError("missing schema")
    with pytest.raises(RuntimeError):
        await store.authorize(IDENTITY)


def test_migration_preserves_ingress_and_requires_explicit_binding_membership():
    path = Path(__file__).resolve().parents[1] / "db/migrations/2026-09-17-zalo-bot-roundtrip.sql"
    statements = split_statements(path.read_text(encoding="utf-8"))
    ddl = [" ".join(stmt.sql.lower().split()) for stmt in statements if stmt.kind == "DDL"]
    assert len(ddl) == 2
    event_ddl, binding_ddl = ddl
    assert event_ddl.startswith("alter table zalo_bot_events ")
    columns = re.findall(r"add column if not exists (\w+) (\w+)", event_ddl)
    assert dict(columns) == {
        "identity_key": "text", "source_event_key": "text", "assistant_outcome": "jsonb",
        "outbound_message_id": "text", "completed_at": "timestamptz",
    }
    assert binding_ddl.startswith("create table if not exists zalo_sales_bindings (")
    declarations = binding_ddl.split("(", 1)[1].rsplit(")", 1)[0].split(",")
    assert [column.strip() for column in declarations] == [
        "opaque_identity text primary key",
        "sales_id bigint not null references sales(id) on delete restrict",
        "is_active boolean not null",
    ]
    assert [stmt.kind for stmt in statements] == ["BEGIN", "DDL", "DDL", "COMMIT"]
