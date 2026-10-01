"""Tests for the sql_leg RLS transaction helper (with_rls_identity).

Mimosa security finding: SET LOCAL ROLE interpolated an unvalidated string.
These tests pin the fail-closed allowlist — a rejected role must raise
SpecError before any pool/connection work happens, so no statement is ever
built from attacker-influenced text. No DB/network.
"""

import re

import pytest

from api.application.services import sql_leg
from api.application.services.sql_leg import (
    ALLOWED_RLS_ROLES,
    SpecError,
    run_sql_leg,
    validate_spec,
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
        self.fetched: list[tuple[str, list]] = []
        self.transaction_started = False

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    async def execute(self, sql: str, *args) -> None:
        self.executed.append(sql)

    async def fetch(self, sql: str, *args):
        self.fetched.append((sql, list(args)))
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


def test_validate_spec_folds_a_top_level_subject_type_into_one_bound_filter():
    normalized = validate_spec(
        {"source": "facts", "subject_type": " unit ", "filters": [], "limit": 4}
    )
    assert normalized["filters"] == [{"field": "subject_type", "op": "=", "value": "unit"}]
    assert "subject_type" not in normalized


def test_validate_spec_keeps_the_offers_subject_type_at_the_top_level():
    """The offers view owns its own subject_type: folding it would drop the scope."""
    normalized = validate_spec(
        {"source": "v_unit_offers", "subject_type": "unit", "filters": [], "limit": 4}
    )
    assert normalized["subject_type"] == "unit"
    assert normalized["filters"] == []


def test_validate_spec_never_mutates_the_caller_spec():
    spec = {"source": "facts", "subject_type": "unit", "filters": [], "limit": 4}
    validate_spec(spec)
    assert spec["filters"] == []
    assert spec["subject_type"] == "unit"


@pytest.mark.parametrize("bad", ["", "   ", 42, None, ["unit"]])
def test_validate_spec_rejects_an_unusable_subject_type(bad):
    with pytest.raises(SpecError):
        validate_spec({"source": "facts", "subject_type": bad, "filters": [], "limit": 4})


def test_validate_spec_rejects_a_subject_type_already_given_as_a_filter():
    """Two competing scopes is an ambiguity, never a precedence to break silently."""
    with pytest.raises(SpecError):
        validate_spec(
            {
                "source": "facts",
                "subject_type": "unit",
                "filters": [{"field": "subject_type", "op": "=", "value": "project"}],
                "limit": 4,
            }
        )


@pytest.mark.asyncio
async def test_run_sql_leg_binds_the_subject_type_as_a_parameter(monkeypatch):
    pool = _FakePool()

    async def fake_get_ro_pool():
        return pool

    monkeypatch.setattr(sql_leg, "get_ro_pool", fake_get_ro_pool)
    result = await run_sql_leg(
        {"source": "facts", "subject_type": "unit", "filters": [], "limit": 3},
        None,
        "giá bao nhiêu",
        "camellia",
    )
    assert result.degraded is False
    sql, args = pool.connection.fetched[-1]
    slot = re.search(r"fs\.subject_type = \$(\d+)", sql)
    assert slot is not None
    assert args[int(slot.group(1)) - 1] == "unit"
    assert sql.count("fs.subject_type =") == 1


@pytest.mark.asyncio
async def test_run_sql_leg_never_acquires_a_pool_for_an_invalid_spec(monkeypatch):
    async def exploding_pool():
        raise AssertionError("an invalid spec must not reach the pool")

    monkeypatch.setattr(sql_leg, "get_ro_pool", exploding_pool)
    result = await run_sql_leg(
        {"source": "facts", "subject_type": "", "filters": [], "limit": 3},
        None,
        "giá bao nhiêu",
        "camellia",
    )
    assert result.degraded is True
    assert result.meta["degraded_reason"] == "spec_invalid"


@pytest.mark.asyncio
async def test_probe_ignores_historical_as_of_and_uses_current_date(monkeypatch):
    """A historical as_of must not leak into the probe: SAME window as P0-04 (CURRENT_DATE)."""
    from datetime import date

    from api.application.services.sql_leg import probe_confirmed_price

    captured: dict = {}

    class Conn:
        async def fetchrow(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return {"any_confirmed": False, "unit_keys": None}

    class Manager:
        def __init__(self, conn):
            self._conn = conn

        async def __aenter__(self):
            return self._conn

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(sql_leg, "with_rls_identity", lambda **kwargs: Manager(Conn()))
    probe = await probe_confirmed_price("camellia", ["CH-03"], as_of=date(2020, 1, 1))
    assert probe.completed is True
    assert "CURRENT_DATE" in captured["sql"]
    assert date(2020, 1, 1) not in captured["params"]
    assert captured["params"][0] == "camellia"


def test_probe_sql_has_no_date_bind_slot():
    """Opposite control: no $2 date slot remains for a caller-supplied window."""
    import re

    from api.application.services.sql_leg import _PROBE_CONFIRMED_PRICE_SQL

    sql = _PROBE_CONFIRMED_PRICE_SQL.format(unit_filter="", unit_limit=200)
    assert re.search(r"\$\d+", sql) is None or "$1" in sql and "$2" not in sql
