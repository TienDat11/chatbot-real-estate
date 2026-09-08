"""Regression: the admin-deps sales mapping prefers firebase_uid, then access_key.

The PG read seam in ``api.interfaces.api.deps.admin`` is exercised against a
fake psycopg2 module so no database is needed: a uid-matched row short-circuits
after one query, while legacy rows (uid lookup misses) fall back to the
access_key query that keeps pre-provisioning sessions working.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from api.infrastructure.config.config import get_settings
from api.interfaces.api.deps import admin as admin_deps


class _FakeCursor:
    """Records executed statements; fetchone pops from a scripted row queue."""

    def __init__(self, scripted_rows: list[tuple | None]) -> None:
        self._scripted_rows = list(scripted_rows)
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple | None:
        return self._scripted_rows.pop(0)

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class _FakePsycopg2Module:
    """sys.modules stand-in exposing only connect()."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self.connect_calls: list[tuple[str, int]] = []

    def connect(self, dsn: str, connect_timeout: int = 0) -> _FakeConnection:
        self.connect_calls.append((dsn, connect_timeout))
        return self._connection


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    """Install the fake psycopg2 module and hand back the shared cursor."""
    cursor = _FakeCursor(scripted_rows=[])
    module = _FakePsycopg2Module(_FakeConnection(cursor))
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    monkeypatch.setattr(get_settings(), "sales_legacy_key_auth_enabled", True)
    return cursor


def test_resolves_by_firebase_uid_first(fake_pg: _FakeCursor) -> None:
    fake_pg._scripted_rows.append((7,))
    assert admin_deps._fetch_active_sales_id_sync("uid-abc") == 7
    assert len(fake_pg.executed) == 1
    sql, params = fake_pg.executed[0]
    assert "firebase_uid" in sql
    assert "access_key" not in sql
    assert params == ("uid-abc",)


def test_falls_back_to_access_key_when_uid_lookup_misses(
    fake_pg: _FakeCursor,
) -> None:
    # Legacy row: no firebase_uid match, but access_key still doubles as the uid.
    fake_pg._scripted_rows.extend([None, (3,)])
    assert admin_deps._fetch_active_sales_id_sync("key-legacy") == 3
    assert len(fake_pg.executed) == 2
    fallback_sql, fallback_params = fake_pg.executed[1]
    assert "access_key = %s" in fallback_sql
    assert fallback_params == ("key-legacy",)


def test_empty_uid_skips_uid_query(fake_pg: _FakeCursor) -> None:
    fake_pg._scripted_rows.append(None)
    assert admin_deps._fetch_active_sales_id_sync("") is None
    assert len(fake_pg.executed) == 1
    assert "access_key" in fake_pg.executed[0][0]
