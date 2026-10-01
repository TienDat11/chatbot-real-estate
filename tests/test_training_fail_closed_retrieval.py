"""Active ProjectContext scope reaches workflow data consumers."""

from __future__ import annotations

import asyncio

from api.application.pipelines import workflow as wf_mod
from api.application.pipelines.workflow import RagQueryWorkflow
from api.application.services.estimates_fallback import apply_estimates_fallback
from api.application.services.merge import Merged
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult
from api.application.services.sql_leg import SqlLegResult
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.rewrite import RoutedResult


# All workflow legs run so every ProjectContext consumer is exercised.
_ROUTING_ALL = {
    "needs_rag": True,
    "needs_sql": True,
    "structured_path": "spec",
    "needs_geo": True,
}


class _FakeGeo:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def places_around(self, lat, lng, radius_m):
        from api.infrastructure.ports.geo import GeoResult

        self._calls.append((lat, lng, radius_m))
        return GeoResult([], degraded=False)


class _FakeReranker:
    async def rerank(self, query, chunks):
        return chunks


def _make_merged(**meta) -> Merged:
    return Merged(rag_blocks="", evidence_blocks="", sources=[], facts=[], meta=dict(meta))


def _install_seams(monkeypatch, rewritten: str = "hoi tra cuu"):
    """Patch every external seam of the 8-step workflow with recording fakes.

    Returns the shared recorder dict: rag_calls, sql_calls, geo_calls,
    search_images_calls, merge_scopes, events, audits.
    """
    rec: dict = {
        "rag_calls": [],
        "sql_calls": [],
        "geo_calls": [],
        "search_images_calls": [],
        "fetch_board_calls": [],
        "merge_scopes": [],
        "events": [],
        "audits": [],
    }

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=False)

    async def fake_rewrite(clean, history, as_of_iso):
        return RoutedResult(
            rewritten=rewritten,
            routing=dict(_ROUTING_ALL),
            sql_spec={"source": "facts", "filters": [], "limit": 5},
            hl_keywords=[],
            ll_keywords=[],
            high_stakes=False,
            as_of=None,
        )

    async def fake_rag(rewritten_, hl, ll, as_of, project_key=None):
        rec["rag_calls"].append(project_key)
        return RagLegResult([], degraded=False)

    async def fake_sql(spec, as_of, clean, project_key=None):
        rec["sql_calls"].append(project_key)
        return SqlLegResult([], {"mode": "spec"}, degraded=False)

    async def fake_merge(query, chunks, rows, as_of, project_key=None):
        rec["merge_scopes"].append(project_key)
        merged = _make_merged()
        rec["merged"] = merged
        return merged
    async def fake_search(*args, **kwargs):
        rec["search_images_calls"].append((args, kwargs))
        return []

    async def fake_fetch_board(key):
        rec["fetch_board_calls"].append(key)
        return []

    async def fake_stream(merged, history, high_stakes):
        rec["prompt_meta"] = dict(merged.meta)
        yield "ok"

    async def fake_output_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="LOW", requires_review=False, verdicts={})

    async def fake_audit(entry):
        rec["audits"].append(entry)

    monkeypatch.setattr(wf_mod, "guard_input", fake_guard)
    monkeypatch.setattr(wf_mod, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(wf_mod, "run_rag_leg", fake_rag)
    monkeypatch.setattr(wf_mod, "run_sql_leg", fake_sql)
    monkeypatch.setattr(wf_mod, "merge_context", fake_merge)
    monkeypatch.setattr(wf_mod, "search_images", fake_search)
    monkeypatch.setattr(wf_mod, "fetch_price_board_images", fake_fetch_board)
    monkeypatch.setattr(wf_mod, "is_full_price_board_query", lambda q: False)
    monkeypatch.setattr(wf_mod, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(wf_mod, "request_project_snapshot", lambda: None)
    monkeypatch.setattr(wf_mod, "get_geo", lambda: _FakeGeo(rec["geo_calls"]))
    monkeypatch.setattr(wf_mod, "project_geo_center", lambda k: (16.1, 108.2))
    monkeypatch.setattr(wf_mod, "stream_answer", fake_stream)
    monkeypatch.setattr(wf_mod, "guard_output", fake_output_guard)
    monkeypatch.setattr(wf_mod, "write_audit", fake_audit)
    return rec


def _run(monkeypatch, *, project_context):
    rec = _install_seams(monkeypatch)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: rec["events"].append((e, d)))
        return await wf.run(
            query="hoi",
            session_id=None,
            history=[],
            project_context=project_context,
        )

    return asyncio.run(go()), rec


# --- 1. Training and customer active-scope contracts ----------------------------


def test_training_scope_reaches_every_data_consumer(monkeypatch) -> None:
    context = ProjectContext("camellia", "training", (("camellia", "Camellia"),))
    result, rec = _run(monkeypatch, project_context=context)

    assert rec["rag_calls"] == ["camellia"]
    assert rec["sql_calls"] == ["camellia"]
    assert rec["merge_scopes"] == ["camellia"]
    assert rec["geo_calls"]
    assert rec["search_images_calls"][0][1]["project_key"] == "camellia"
    assert rec["prompt_meta"]["project_key"] == "camellia"
    assert rec["prompt_meta"]["answer_mode"] == "training"
    assert result["sources"] == []


def test_customer_active_scope_reaches_data_consumers(monkeypatch) -> None:
    context = ProjectContext("camellia", "normal", (("camellia", "Camellia"),))
    _, rec = _run(monkeypatch, project_context=context)

    assert rec["rag_calls"] == ["camellia"]
    assert rec["sql_calls"] == ["camellia"]
    assert rec["merge_scopes"] == ["camellia"]
    assert rec["geo_calls"]
    assert rec["search_images_calls"][0][1]["project_key"] == "camellia"
    assert rec["prompt_meta"]["project_key"] == "camellia"
    assert rec["prompt_meta"]["answer_mode"] == "normal"


# --- 2. Estimates fallback uses the active project scope ------------------------


def test_estimates_fallback_uses_active_project_scope(monkeypatch) -> None:
    from api.application.services import estimates_fallback as ef

    calls: list = []

    async def fake_fetch(project_key):
        calls.append(project_key)
        return []

    monkeypatch.setattr(ef, "fetch_unit_estimates", fake_fetch)

    m = _make_merged(
        project_key="camellia",
        retrieval_project_key="camellia",
        answer_mode="training",
        query="giá căn hộ bao nhiêu",
        rewritten="giá căn hộ camellia",
    )
    asyncio.run(apply_estimates_fallback(m))
    assert calls == ["camellia"]


    # Legacy merged frames without the retrieval key keep the old behavior.
    calls.clear()
    m3 = _make_merged(
        project_key="soleil", query="giá căn hộ bao nhiêu", rewritten="giá căn hộ soleil"
    )
    asyncio.run(apply_estimates_fallback(m3))
    assert calls == ["soleil"]


def _record(project_key: str):
    from api.application.ports.project_registry import ProjectRegistryRecord

    return ProjectRegistryRecord(
        project_key=project_key, ten_thuong_mai=project_key, ten_phap_ly=None,
        vi_tri=None, hotline=None, location=None, geo_center_lat=None,
        geo_center_lng=None, is_hot=False, status="active",
    )


def _run_with_record(monkeypatch, *, project_context, record):
    rec = _install_seams(monkeypatch)
    monkeypatch.setattr(wf_mod, "request_project_snapshot", lambda: record)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: rec["events"].append((e, d)))
        return await wf.run(
            query="hoi",
            session_id=None,
            history=[],
            project_context=project_context,
        )

    return asyncio.run(go()), rec


def test_non_camellia_without_coords_omits_geo_lookup(monkeypatch) -> None:
    """A resolved non-Camellia project with no verified coords never borrows Camellia's center."""
    context = ProjectContext("soleil", "normal", (("soleil", "Soleil"),))
    result, rec = _run_with_record(monkeypatch, project_context=context, record=_record("soleil"))
    assert rec["geo_calls"] == []
    assert result["places"] == []


def test_camellia_without_coords_keeps_legacy_geo_lookup(monkeypatch) -> None:
    """Opposite control: Camellia with no record coords keeps the legacy default lookup."""
    context = ProjectContext("camellia", "normal", (("camellia", "Camellia"),))
    result, rec = _run_with_record(monkeypatch, project_context=context, record=_record("camellia"))
    assert rec["geo_calls"]
    assert result["places"] == []


def test_non_camellia_with_coords_still_looks_up(monkeypatch) -> None:
    """Non-Camellia WITH verified coords keeps its own lookup (guard is fallback-only)."""
    from api.application.ports.project_registry import ProjectRegistryRecord

    context = ProjectContext("soleil", "normal", (("soleil", "Soleil"),))
    record = ProjectRegistryRecord(
        project_key="soleil", ten_thuong_mai="soleil", ten_phap_ly=None,
        vi_tri=None, hotline=None, location=None, geo_center_lat=16.2,
        geo_center_lng=108.3, is_hot=False, status="active",
    )
    result, rec = _run_with_record(monkeypatch, project_context=context, record=record)
    assert rec["geo_calls"] == [(16.2, 108.3, 10000)]
    assert result["places"] == []
