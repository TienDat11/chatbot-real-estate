"""Story 10.4: retrieval scoping by project_key — isolation tests.

A Soleil query must never surface Camellia facts, chunks, or images. These tests
cover the scoping logic that lives in the retrieval layer:

- ``build_sql`` adds a project_key predicate to the facts path (and a subject
  subquery to the v_unit_offers path) — verified on the generated SQL, no DB.
- ``run_affordability`` keeps only the requested project's estimate rows
  (injectable fetch, no DB).
- ``run_rag_leg._post_filter`` appends the documents.project_key predicate and
  drops chunks whose doc belongs to another project (monkeypatched pool).
- ``search_images`` / ``search_project_images`` scope the project predicate in
  SQL (M6): the scoped fetch binds project_key as a query parameter instead
  of fetching every project's rows and post-filtering (monkeypatched conn).

None of these touch a live database; the pool/fetch are replaced.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import patch

import pytest

from api.application.services import image_search as img
from api.application.services.sql_leg import build_sql, run_affordability

# --- build_sql: facts path must carry fs.project_key -------------------------

def test_build_sql_facts_adds_project_key_predicate() -> None:
    spec = {"source": "facts", "filters": [], "limit": 10}
    sql, params = build_sql(spec, date(2026, 8, 1), project_key="soleil")
    assert "fs.project_key = $2" in sql or "fs.project_key" in sql
    assert "soleil" in params


def test_build_sql_facts_without_project_key_omits_predicate() -> None:
    spec = {"source": "facts", "filters": [], "limit": 10}
    sql, _ = build_sql(spec, date(2026, 8, 1))
    assert "fs.project_key" not in sql


def test_build_sql_offers_scopes_to_project_subjects() -> None:
    spec = {"source": "v_unit_offers", "filters": [], "limit": 10}
    sql, params = build_sql(spec, date(2026, 8, 1), project_key="soleil")
    assert "fact_subjects" in sql
    assert "project_key = $2" in sql
    assert "soleil" in params


# --- run_affordability: estimates filtered by project_key --------------------

def _est_row(project_key: str = "camellia") -> dict:
    return {
        "subject_key": f"unit:{project_key}/studio",
        "display_name": "Studio",
        "project_key": project_key,
        "attrs": "{}",
        "policy_key": "chuan",
        "price_min_vnd": 1_900_000_000,
        "price_max_vnd": 2_300_000_000,
        "price_quality": "range",
        "deposit_pct": 30.0,
        "term_months": 18,
        "interest_rate_pct": 0.0,
    }


@pytest.mark.asyncio
async def test_run_affordability_keeps_only_requested_project() -> None:
    async def fake_fetch() -> list[dict]:
        return [_est_row("camellia"), _est_row("soleil")]

    spec = {
        "structured_path": "affordability",
        "source": "v_unit_estimates",
        "budget_vnd": 2_000_000_000,
        "limit": 20,
    }
    result = await run_affordability(spec, None, fetch=fake_fetch, project_key="soleil")
    assert result.degraded is False
    assert len(result.rows) == 1
    assert result.rows[0]["subject"] == "Studio"
    # The surviving row must be the Soleil estimate, not the Camellia one.
    assert result.rows[0]["fields"]["price_min_vnd"] == 1_900_000_000


@pytest.mark.asyncio
async def test_run_affordability_other_project_rows_are_not_counted() -> None:
    async def fake_fetch() -> list[dict]:
        return [_est_row("camellia")]

    spec = {
        "structured_path": "affordability",
        "source": "v_unit_estimates",
        "budget_vnd": 2_000_000_000,
        "limit": 20,
    }
    result = await run_affordability(spec, None, fetch=fake_fetch, project_key="soleil")
    # Camellia estimates must not price a Soleil budget question.
    assert result.rows == []
    assert result.degraded is True  # no estimates survive the project filter


# --- rag_leg._post_filter: chunks scoped by documents.project_key -------------

@pytest.mark.asyncio
async def test_post_filter_drops_chunks_of_other_project() -> None:
    from api.application.services import rag_leg

    chunks = [
        {"id": "c-camellia", "score": 0.9, "content": "Camellia price", "file_path": "c-camellia"},
        {"id": "c-soleil", "score": 0.8, "content": "Soleil price", "file_path": "c-soleil"},
    ]
    recs = [
        {"chunk_id": "c-camellia", "doc_id": "d1", "status": "published",
         "effective_from": date(2026, 1, 1), "effective_to": None, "project_key": "camellia"},
        {"chunk_id": "c-soleil", "doc_id": "d2", "status": "published",
         "effective_from": date(2026, 1, 1), "effective_to": None, "project_key": "soleil"},
    ]

    class _FakePool:
        # asyncpg Pool.acquire is a sync method returning a holder that is both
        # awaitable and an async CM; mirror that so `async with pool.acquire()`
        # resolves to the fake connection.
        def acquire(self):
            return _FakeConn(recs)

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch(self, sql, *args):
            assert "d.project_key" in sql  # project predicate must be in the SQL
            return self._rows

    with patch.object(rag_leg, "get_ro_pool", return_value=_FakePool()):
        kept = await rag_leg._post_filter(chunks, None, project_key="soleil")

    assert [c["id"] for c in kept] == ["c-soleil"]


@pytest.mark.asyncio
async def test_post_filter_without_project_key_keeps_all_valid() -> None:
    from api.application.services import rag_leg

    chunks = [{"id": "c-1", "score": 0.9, "content": "x", "file_path": "c-1"}]
    recs = [
        {"chunk_id": "c-1", "doc_id": "d1", "status": "published",
         "effective_from": date(2026, 1, 1), "effective_to": None, "project_key": "camellia"},
    ]

    class _FakePool:
        def acquire(self):
            return _FakeConn(recs)

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch(self, sql, *args):
            return self._rows

    with patch.object(rag_leg, "get_ro_pool", return_value=_FakePool()):
        kept = await rag_leg._post_filter(chunks, None)
    assert [c["id"] for c in kept] == ["c-1"]


# --- image search: project predicate rides in the SQL (M6) --------------------

def _capture_conn(rows, captured):
    class _CaptureConn:
        async def fetch(self, *args, **kwargs):
            captured["args"] = args
            return rows

        async def fetchrow(self, *args, **kwargs):
            captured["args"] = args
            return None

    @asynccontextmanager
    async def rls():
        yield _CaptureConn()

    return rls


@pytest.mark.asyncio
async def test_search_images_scopes_project_in_sql(monkeypatch) -> None:
    """A scoped search binds project_key as a query parameter on the scoped SQL
    variant — it must never fetch the cross-project corpus and post-filter."""
    captured: dict = {}

    async def fake_embed(text):
        return [0.5]

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    monkeypatch.setattr(img, "with_rls_identity", _capture_conn([], captured))
    await img.search_images("view biển", top_k=4, project_key="soleil")

    query, vec_literal, pool, project_key = captured["args"]
    assert query == img.IMAGE_QUERY_PROJECT_SCOPED
    assert "i.project_key = $3" in query
    assert vec_literal == "[0.5]"
    assert pool == 8
    assert project_key == "soleil"


@pytest.mark.asyncio
async def test_search_images_without_project_stays_unscoped(monkeypatch) -> None:
    """No project bound -> the legacy unscoped query and args are unchanged."""
    captured: dict = {}

    async def fake_embed(text):
        return [0.5]

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    monkeypatch.setattr(img, "with_rls_identity", _capture_conn([], captured))
    await img.search_images("view biển", top_k=4)

    query, vec_literal, pool = captured["args"]
    assert query == img.IMAGE_QUERY
    assert "project_key" not in query


@pytest.mark.asyncio
async def test_search_project_images_scopes_project_in_sql(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(img, "with_rls_identity", _capture_conn([], captured))
    await img.search_project_images(project_key="soleil")

    query, kind, order_str, top_k, project_key = captured["args"]
    assert query == img.PROJECT_IMAGES_QUERY_PROJECT_SCOPED
    assert "i.project_key = $4" in query
    assert project_key == "soleil"


@pytest.mark.asyncio
async def test_scoped_unit_rescue_honors_project_predicate(monkeypatch) -> None:
    """Unit codes are not unique across projects: the CH-03 rescue lookup must
    carry the project predicate too (a Soleil CH-03 question must never be
    rescued into Camellia's CH-03 floor plan)."""
    captured: dict = {}
    monkeypatch.setattr(img, "with_rls_identity", _capture_conn([], captured))
    await img._query_by_unit("CH-03", project_key="soleil")

    query, unit_key, code, project_key = captured["args"]
    assert query == img.QUERY_BY_UNIT_PROJECT_SCOPED
    assert "i.project_key = $3" in query
    assert project_key == "soleil"
    assert unit_key == "unit:CH-03"
