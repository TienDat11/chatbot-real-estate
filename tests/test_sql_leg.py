"""Tests for the sql_leg RLS transaction helper (with_rls_identity).

Mimosa security finding: SET LOCAL ROLE interpolated an unvalidated string.
These tests pin the fail-closed allowlist — a rejected role must raise
SpecError before any pool/connection work happens, so no statement is ever
built from attacker-influenced text. No DB/network.
"""

import pytest

from api.application.services.sql_leg import (
    ALLOWED_RLS_ROLES,
    SpecError,
    with_rls_identity,
)


class _FakeTransaction:
    def __init__(self, conn: "_FakeConnection") -> None:
        self._conn = conn
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self._conn.transaction_started = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.transaction_started = False

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(sql)

    async def fetch(self, sql: str, *args):
        return []


class _FakePool:
    def __init__(self) -> None:
        self.connection = _FakeConnection()
        self.acquire_calls = 0
        self.released: list[_FakeConnection] = []

    async def acquire(self) -> _FakeConnection:
        self.acquire_calls += 1
        return self.connection

    async def release(self, conn: _FakeConnection) -> None:
        self.released.append(conn)


@pytest.mark.asyncio
async def test_rejects_disallowed_role_before_touching_any_connection():
    """A role outside the allowlist fails closed with zero connection activity."""
    pool = _FakePool()
    with pytest.raises(SpecError, match="role"):
        async with with_rls_identity(role="superuser", pool=pool):
            pytest.fail("must not yield a connection for a disallowed role")
    assert pool.acquire_calls == 0
    assert not pool.connection.transaction_started
    assert pool.connection.executed == []


@pytest.mark.asyncio
async def test_rejects_injection_attempt_in_role_parameter():
    """Multi-statement payloads are just strings — never in the allowlist."""
    pool = _FakePool()
    with pytest.raises(SpecError):
        async with with_rls_identity(role="ro_query; DROP TABLE facts", pool=pool):
            pytest.fail("injection payload must be rejected")
    assert pool.acquire_calls == 0
    assert pool.connection.executed == []


@pytest.mark.asyncio
async def test_allowlisted_role_runs_and_sets_local_role():
    """The default ro_query path still opens one transaction and sets role+timeout."""
    pool = _FakePool()
    async with with_rls_identity(timeout_s=1.5, pool=pool) as conn:
        await conn.fetch("SELECT 1")
    assert pool.acquire_calls == 1
    # One release per acquire keeps the RO pool from leaking connections.
    assert pool.released == [pool.connection]
    assert conn.executed == [
        "SELECT set_config('statement_timeout', $1, true)",
        "SELECT set_config('role', $1, true)",
    ]


def test_allowlist_stays_minimal_and_immutable():
    """The closed set holds exactly the role every caller uses today."""
    assert isinstance(ALLOWED_RLS_ROLES, frozenset)
    assert ALLOWED_RLS_ROLES == frozenset({"ro_query"})
