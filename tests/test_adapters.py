"""Adapter contract tests — no network/DB; fakes replace external calls."""

from __future__ import annotations

import pytest

from api.adapters.google_places import GooglePlaces
from api.adapters.lightrag import LightRag
from api.adapters.postgres_sql import PostgresSql
from api.adapters.static_places import StaticPlaces
from api.ports.geo import GeoResult
from api.ports.rag import RagResult
from api.ports.sql import SqlResult

_CATALOG = "db/seed/static_places.json"


@pytest.mark.asyncio
async def test_static_places_filters_by_kinds_and_radius():
    sp = StaticPlaces(_CATALOG, radius_m=10000)
    res = await sp.places_around(16.0558, 108.2455, 10000, kinds=["school", "hospital"])
    assert isinstance(res, GeoResult)
    assert not res.degraded
    assert res.places
    assert all(set(p.kinds) & {"school", "hospital"} for p in res.places)


@pytest.mark.asyncio
async def test_static_places_unknown_kind_returns_empty():
    sp = StaticPlaces(_CATALOG)
    res = await sp.places_around(0, 0, 10000, kinds=["gym"])
    assert res.places == []


@pytest.mark.asyncio
async def test_static_places_missing_catalog_returns_empty():
    sp = StaticPlaces("db/seed/__missing__.json")
    res = await sp.places_around(0, 0, 10000)
    assert res.places == []


@pytest.mark.asyncio
async def test_google_places_missing_config_degrades():
    gp = GooglePlaces(api_key="", base_url="")
    res = await gp.places_around(16.0, 108.0, 5000)
    assert res.degraded
    assert res.places == []


@pytest.mark.asyncio
async def test_google_places_network_failure_degrades(monkeypatch):
    class _FakeClient:
        def __init__(self, *, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            raise OSError("network down")

    monkeypatch.setattr("api.adapters.google_places.httpx.AsyncClient", _FakeClient)
    gp = GooglePlaces(api_key="key", base_url="https://example.invalid")
    res = await gp.places_around(16.0, 108.0, 5000)
    assert res.degraded
    assert res.places == []


@pytest.mark.asyncio
async def test_lightrag_init_failure_degrades(monkeypatch):
    async def _boom():
        raise RuntimeError("no lightrag")

    monkeypatch.setattr("api.adapters.lightrag._get_rag", _boom)
    rag = LightRag()
    res = await rag.retrieve("có bao nhiêu căn")
    assert isinstance(res, RagResult)
    assert res.degraded
    assert res.chunks == []


@pytest.mark.asyncio
async def test_postgres_sql_rows_from_spec_delegates(monkeypatch):
    from api.sql_leg import SqlLegResult

    calls: list = []

    async def _fake_spec(spec, as_of, query):
        calls.append((spec, as_of, query))
        return SqlLegResult([{"fe_id": "fe-001"}], {"mode": "spec"}, degraded=False)

    monkeypatch.setattr("api.adapters.postgres_sql.run_sql_leg", _fake_spec)
    sql = PostgresSql()
    res = await sql.rows_from_spec({"source": "facts"}, None)
    assert isinstance(res, SqlResult)
    assert res.rows == [{"fe_id": "fe-001"}]
    assert calls[0][0] == {"source": "facts"}


@pytest.mark.asyncio
async def test_postgres_sql_rows_from_nl2sql_delegates(monkeypatch):
    from api.sql_leg import SqlLegResult

    async def _fake_nl2sql(query, as_of):
        return SqlLegResult([{"fe_id": "fe-001"}], {"mode": "nl2sql"}, degraded=False)

    monkeypatch.setattr("api.nl2sql_guard.run_nl2sql", _fake_nl2sql)
    sql = PostgresSql()
    res = await sql.rows_from_nl2sql("có bao nhiêu căn", None)
    assert res.meta["mode"] == "nl2sql"
    assert res.rows