"""Offline checks of trusted provisioning and fresh ACTIVE authorization."""

from contextlib import asynccontextmanager

import pytest

from api.application.services.zalo_bot_identity import opaque_identity
from api.infrastructure.adapters import postgres_zalo_bindings as bindings

SECRET = "binding-test-secret-not-production-123"
CHAT = "private-chat"
IDENTITY = opaque_identity(CHAT, SECRET)


class FakeConnection:
    def __init__(self):
        self.sales = {7: True, 8: True, 9: False}
        self.bindings = {}
        self.error = None
        self.query_count = 0
        self.in_transaction = False

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def fetchrow(self, query, *args, timeout):
        assert timeout > 0
        self.query_count += 1
        if self.error:
            raise self.error
        sql = " ".join(query.split())
        if "JOIN sales AS s" in sql:
            assert "s.id = b.sales_id" in sql
            assert "b.opaque_identity = $1" in sql
            assert "b.is_active = TRUE AND s.is_active = TRUE" in sql
            row = self.bindings.get(args[0])
            if row and row["is_active"] and self.sales.get(row["sales_id"], False):
                return {"sales_id": row["sales_id"]}
            return None
        assert self.in_transaction
        if sql.startswith("SELECT id FROM sales"):
            assert "is_active = TRUE FOR UPDATE" in sql
            return {"id": args[0]} if self.sales.get(args[0], False) else None
        if sql.startswith("SELECT sales_id FROM zalo_sales_bindings"):
            assert "opaque_identity = $1 FOR UPDATE" in sql
            return self.bindings.get(args[0])
        if sql.startswith("UPDATE zalo_sales_bindings"):
            row = self.bindings.get(args[0])
            if row is None or (len(args) == 2 and row["sales_id"] != args[1]):
                return None
            if len(args) == 2:
                assert "sales_id = $2" in sql
            row["is_active"] = "SET is_active = TRUE" in sql
            return {"sales_id": row["sales_id"]}
        raise AssertionError(f"Unexpected query: {sql}")

    async def execute(self, query, *args, timeout):
        assert self.in_transaction and timeout > 0
        assert "VALUES ($1, $2, TRUE)" in query
        assert "ON CONFLICT (opaque_identity) DO NOTHING" in query
        self.bindings.setdefault(args[0], {"sales_id": args[1], "is_active": True})
        return "INSERT 0 1"


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

    monkeypatch.setattr(bindings, "get_lead_pool", get_pool)
    return conn


@pytest.mark.asyncio
async def test_lifecycle_preserves_owner_and_requires_explicit_reenable(connection):
    assert await bindings.authorize(IDENTITY) is None
    assert await bindings.provision_binding(CHAT, 7, SECRET, ownership_verified=True)
    assert await bindings.authorize(IDENTITY) == 7
    assert await bindings.provision_binding(CHAT, 7, SECRET, ownership_verified=True)
    assert not await bindings.provision_binding(CHAT, 8, SECRET, ownership_verified=True)
    assert await bindings.authorize(IDENTITY) == 7
    assert await bindings.revoke_binding(IDENTITY)
    assert await bindings.authorize(IDENTITY) is None
    assert await bindings.provision_binding(CHAT, 7, SECRET, ownership_verified=True)
    assert await bindings.authorize(IDENTITY) is None
    assert not await bindings.reenable_binding(IDENTITY, 8, approved=True)
    assert await bindings.authorize(IDENTITY) is None
    assert await bindings.reenable_binding(IDENTITY, 7, approved=True)
    assert await bindings.authorize(IDENTITY) == 7


@pytest.mark.asyncio
async def test_current_sales_state_controls_authorization_and_approval(connection):
    assert not await bindings.provision_binding(CHAT, 9, SECRET, ownership_verified=True)
    assert not await bindings.provision_binding(CHAT, 99, SECRET, ownership_verified=True)
    assert await bindings.provision_binding(CHAT, 7, SECRET, ownership_verified=True)
    connection.sales[7] = False
    assert await bindings.authorize(IDENTITY) is None
    assert not await bindings.provision_binding(CHAT, 7, SECRET, ownership_verified=True)
    assert await bindings.revoke_binding(IDENTITY)
    assert not await bindings.reenable_binding(IDENTITY, 7, approved=True)
    connection.sales[7] = True
    assert await bindings.authorize(IDENTITY) is None
    assert await bindings.reenable_binding(IDENTITY, 7, approved=True)
    del connection.sales[7]
    assert await bindings.authorize(IDENTITY) is None


@pytest.mark.asyncio
async def test_unknown_binding_cannot_be_reenabled_or_revoked(connection):
    assert not await bindings.reenable_binding(IDENTITY, 7, approved=True)
    assert not await bindings.revoke_binding(IDENTITY)
    assert await bindings.authorize(IDENTITY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, 1, "true", None])
async def test_self_assertion_and_truthy_attestation_do_not_provision(connection, approval):
    with pytest.raises(ValueError):
        await bindings.provision_binding(CHAT, 7, SECRET, ownership_verified=approval)
    with pytest.raises(ValueError):
        await bindings.reenable_binding(IDENTITY, 7, approved=approval)
    assert connection.bindings == {}
    assert connection.query_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sales_id", [True, 0, -1, "7", 2**63])
async def test_invalid_sales_id_never_reaches_storage(connection, sales_id):
    with pytest.raises(ValueError):
        await bindings.provision_binding(CHAT, sales_id, SECRET, ownership_verified=True)
    with pytest.raises(ValueError):
        await bindings.reenable_binding(IDENTITY, sales_id, approved=True)
    assert connection.bindings == {}
    assert connection.query_count == 0


@pytest.mark.asyncio
async def test_storage_failures_are_not_unknown_membership_and_do_not_leak(connection):
    connection.error = RuntimeError("private-chat sensitive-dsn missing-table")
    with pytest.raises(RuntimeError) as caught:
        await bindings.authorize(IDENTITY)
    assert "private-chat" not in str(caught.value)
    assert "sensitive-dsn" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.asyncio
async def test_pool_acquisition_is_bounded(monkeypatch):
    import asyncio

    async def unavailable_pool():
        await asyncio.Future()

    monkeypatch.setattr(bindings, "get_lead_pool", unavailable_pool)
    monkeypatch.setattr(bindings, "_DB_TIMEOUT", 0.01)
    with pytest.raises(RuntimeError):
        await bindings.authorize(IDENTITY)


@pytest.mark.asyncio
async def test_external_cancellation_is_not_swallowed(connection):
    import asyncio

    connection.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await bindings.authorize(IDENTITY)
