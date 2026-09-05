"""FR-25 review fix: an unresolved training turn must fail CLOSED in the workflow.

Contract locked here (Reviewer wave, 2026-09):

1. training marker + NO context project -> run_rag_leg/run_sql_leg/get_geo/
   image seams are NEVER called: a None project scope would drop the
   ``d.project_key = $2`` predicate in rag_leg._post_filter and retrieve the
   FULL corpus (every project's chunks together). Zero sources, zero facts,
   zero images, zero places, and the loud ``training_scope_unresolved``
   degrade flag rides into the audit.
2. training marker + real context project -> every data consumer receives the
   RESOLVED project ('camellia'), never the '_training' marker: retrieval,
   SQL, merge_context placeholder hydration, and the estimates fallback
   (meta.retrieval_project_key).
3. customer mode with project_key=None keeps its legacy unscoped contract
   (single-active default rule, story 10.1) — the fail-closed guard is
   training-only and must not change it.

All offline: every leg/service seam is a recording fake — no DB, no LLM.
"""

from __future__ import annotations

import asyncio

from api.application.pipelines import workflow as wf_mod
from api.application.pipelines.workflow import (
    RagQueryWorkflow,
    training_retrieval_scope,
)
from api.application.services.estimates_fallback import apply_estimates_fallback
from api.application.services.merge import Merged
from api.application.services.rag_leg import RagLegResult
from api.application.services.sql_leg import SqlLegResult
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.rewrite import RoutedResult

# All three legs on so every fail-closed guard must be exercised at once.
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
        # Reaching this seam at all with a training-unresolved turn is the bug;
        # record the scope so resolved tests can assert on it.
        rec["rag_calls"].append(project_key)
        return RagLegResult([], degraded=False)

    async def fake_sql(spec, as_of, clean, project_key=None):
        rec["sql_calls"].append(project_key)
        return SqlLegResult([], {"mode": "spec"}, degraded=False)

    async def fake_merge(query, chunks, rows, as_of, project_key=None):
        rec["merge_scopes"].append(project_key)
        return _make_merged()

    async def fake_search(*args, **kwargs):
        rec["search_images_calls"].append((args, kwargs))
        return []

    async def fake_fetch_board(key):
        rec["fetch_board_calls"].append(key)
        return []

    async def fake_stream(merged, history, high_stakes):
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


def _run(monkeypatch, *, project_key, training_context):
    rec = _install_seams(monkeypatch)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: rec["events"].append((e, d)))
        return await wf.run(
            query="hoi",
            session_id=None,
            history=[],
            project_key=project_key,
            training_context_project_key=training_context,
        )

    return asyncio.run(go()), rec


# --- 1. training + NO context: zero retrieval anywhere --------------------------


def test_training_unresolved_calls_no_retrieval_seams(monkeypatch) -> None:
    result, rec = _run(monkeypatch, project_key="_training", training_context=None)

    # Absolute fail-closed: not one data seam was entered.
    assert rec["rag_calls"] == []
    assert rec["sql_calls"] == []
    assert rec["geo_calls"] == []
    assert rec["search_images_calls"] == []
    assert rec["fetch_board_calls"] == []
    # Nothing of any project reaches the answer contract.
    assert result["sources"] == []
    assert result["facts"] == []
    assert result["images"] == []
    assert result["places"] == []
    # Loud degrade flag rides into the audit (the ONLY way an operator sees it).
    assert "training_scope_unresolved" in rec["audits"][-1]["degraded"]


# --- 2. training + real context: every consumer gets the RESOLVED key -----------


def test_training_resolved_scopes_every_leg_to_real_project(monkeypatch) -> None:
    result, rec = _run(monkeypatch, project_key="_training", training_context="camellia")

    assert rec["rag_calls"] == ["camellia"]  # never the marker
    assert rec["sql_calls"] == ["camellia"]
    assert rec["merge_scopes"] == ["camellia"]  # placeholder hydration scope
    assert rec["geo_calls"]  # geo ran against the resolved context project
    # The marker must still ride the prompt-profile meta (contract with
    # generate.build_messages / select_answer_tier) — but never a data scope.
    assert training_retrieval_scope("_training", "camellia") == "camellia"


# --- 3. customer None scope preserved; training None never reaches the RAG leg --


def test_customer_none_scope_contract_preserved(monkeypatch) -> None:
    _, rec = _run(monkeypatch, project_key=None, training_context=None)

    # Customer mode may legitimately run unscoped (single-active default,
    # story 10.1): the guard is training-only and must not change this.
    assert rec["rag_calls"] == [None]
    assert rec["sql_calls"] == [None]


def test_no_training_path_reaches_run_rag_leg_with_none(monkeypatch) -> None:
    """Static + behavioral proof: for a training marker the ONLY scope value
    that can reach run_rag_leg is a non-empty context project."""
    # Behavior: marker + empty-string context (the hostile shape that would
    # slip past an `is None` check) also fails closed.
    _, rec = _run(monkeypatch, project_key="_training", training_context="")
    assert rec["rag_calls"] == []
    assert rec["sql_calls"] == []
    # The hydration scope is the falsy resolved context — NEVER the marker.
    assert all(s != "_training" for s in rec["merge_scopes"])
    # The resolved scope of any training marker is falsy only when unresolved,
    # and unresolved is exactly the guarded branch.
    assert training_retrieval_scope("_training", None) is None
    assert training_retrieval_scope("_training", "") == ""


# --- 4. estimates fallback reads the RESOLVED key, gates closed on training None -


def test_estimates_fallback_uses_retrieval_key_and_gates_closed(monkeypatch) -> None:
    from api.application.services import estimates_fallback as ef

    calls: list = []

    async def fake_fetch(project_key):
        calls.append(project_key)
        return []

    monkeypatch.setattr(ef, "fetch_unit_estimates", fake_fetch)

    # Training turn, resolved context: fallback must read 'camellia', not '_training'.
    # Query text carries the Vietnamese price-intent hint the gate requires.
    m = _make_merged(
        project_key="_training",
        retrieval_project_key="camellia",
        query="giá căn hộ bao nhiêu",
        rewritten="giá căn hộ camellia",
    )
    asyncio.run(apply_estimates_fallback(m))
    assert calls == ["camellia"]

    # Training turn, unresolved: retrieval key is None -> gate closed, no read
    # (never falls back to the marker, never reads any project's board).
    calls.clear()
    m2 = _make_merged(
        project_key="_training",
        retrieval_project_key=None,
        query="giá căn hộ bao nhiêu",
        rewritten="giá căn hộ",
    )
    assert asyncio.run(apply_estimates_fallback(m2)) is False
    assert calls == []

    # Legacy merged frames without the retrieval key keep the old behavior.
    calls.clear()
    m3 = _make_merged(
        project_key="soleil", query="giá căn hộ bao nhiêu", rewritten="giá căn hộ soleil"
    )
    asyncio.run(apply_estimates_fallback(m3))
    assert calls == ["soleil"]
