"""P0-03B: evidence publication requires completed, current prompt authority."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

import api.application.pipelines.workflow as workflow_module
from api.application.services import merge as merge_module
from api.application.services.project_context import ProjectContext
from api.application.services.generate import build_messages
from api.application.services.rag_leg import RagLegResult
from api.application.services.sql_leg import ConfirmedPriceProbe
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.rewrite import RoutedResult
from api.application.pipelines.workflow import RagQueryWorkflow

AS_OF = date(2026, 9, 29)
PROJECT = ProjectContext("camellia", "normal", (("camellia", "Camellia"),))
CONTENT = "Camellia có vườn cây xanh và lối dạo bộ."
CLAIM = "Camellia có vườn cây xanh theo Hồ sơ tiện ích Camellia."


def _chunk():
    return {"id": "camellia-amenities", "content": CONTENT, "score": 0.91}


def _record():
    return {"chunk_id": "camellia-amenities", "registry_content": CONTENT,
        "doc_id": "camellia-doc", "title": "Hồ sơ tiện ích Camellia",
        "section": "Tiện ích", "kind": "project", "project_key": "camellia",
        "status": "published", "effective_from": AS_OF - timedelta(days=1), "effective_to": None}


def _run(monkeypatch, *, omit_prompt_rows=False, sql_mode="none", sql_error=False, query="Camellia tiện ích", high_stakes=False, guard_fail=None):
    events, prompts = [], []

    async def guard_input(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def rewrite(clean, history, as_of_iso):
        needs_sql = sql_mode != "none"
        return RoutedResult(rewritten=clean, routing={"needs_rag": True, "needs_sql": needs_sql,
            "structured_path": "nl2sql" if needs_sql else "none", "needs_geo": False},
            sql_spec={}, hl_keywords=[], ll_keywords=[], high_stakes=high_stakes, as_of=AS_OF)

    async def rag(*args):
        return RagLegResult(chunks=[_chunk()])

    async def sql(*args):
        return workflow_module.SqlLegResult([], {"mode": "spec" if sql_mode != "none" else "none",
            **({"error": "lookup failed"} if sql_error else {})}, degraded=sql_error)

    async def stream(merged, history, high_stakes):
        prompts.append(merged)
        if not omit_prompt_rows:
            # The production writer of _prompt_rag_rows/_prompt_fact_rows:
            # publication authority is decided against what build_messages
            # actually serialized, not a hand-built imitation of it.
            build_messages(merged, history)
        yield CLAIM

    async def output_guard(answer, facts, sources, routing, meta=None):
        if guard_fail == "exception":
            raise RuntimeError("verifier down")
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    async def audit(entry):
        return None

    async def price_probe(project_key, unit_codes=None, **kwargs):
        return ConfirmedPriceProbe(True, None, frozenset(), False)
    class Connection:
        async def fetch(self, sql_text, ids):
            return [_record()]

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return None

    class Pool:
        def acquire(self): return Acquire()

    async def pool(): return Pool()

    monkeypatch.setattr(workflow_module, "guard_input", guard_input)
    monkeypatch.setattr(workflow_module, "rewrite_query", rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _Reranker())
    monkeypatch.setattr(workflow_module, "run_rag_leg", rag)
    monkeypatch.setattr(workflow_module, "run_sql_leg", sql)
    monkeypatch.setattr(workflow_module, "stream_answer", stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", audit)
    monkeypatch.setattr(workflow_module, "probe_confirmed_price", price_probe)
    monkeypatch.setattr(merge_module, "get_ro_pool", pool)
    monkeypatch.setattr(workflow_module, "search_images", lambda *a, **k: asyncio.sleep(0, result=[]))

    async def go():
        return await RagQueryWorkflow(on_event=lambda e, d: events.append((e, d))).run(
            query=query, session_id=None, history=[], project_context=PROJECT)

    return asyncio.run(go()), events, prompts


class _Reranker:
    async def rerank(self, query, chunks):
        return chunks


def test_current_selected_prompt_evidence_publishes_in_contract_order(monkeypatch):
    result, events, prompts = _run(monkeypatch)
    # merge hydrates effective_from through _iso() (it is json.dumps'ed into
    # RAG_CONTEXT and every SSE metadata frame), so the authority contract is
    # the ISO window, not a date object.
    assert [{key: source[key] for key in ("doc_id", "title", "section", "effective_from")}
            for source in result["sources"]] == [{"doc_id": "camellia-doc",
        "title": "Hồ sơ tiện ích Camellia", "section": "Tiện ích",
        "effective_from": (AS_OF - timedelta(days=1)).isoformat()}]
    names = [name for name, _ in events]
    assert names.index(workflow_module.SSE_EVENT_SOURCES) < names.index(workflow_module.SSE_EVENT_IMAGES) < names.index(workflow_module.SSE_EVENT_TOKEN)
    # No fact rows were selected, so no facts frame may be published.
    assert workflow_module.SSE_EVENT_FACTS not in names
    assert prompts[0].meta["_prompt_rag_rows"][0]["id"] == _chunk()["id"]


def test_omitted_prompt_rows_block_selected_evidence_and_answer(monkeypatch):
    result, events, _ = _run(monkeypatch, omit_prompt_rows=True)
    assert not result["sources"] and not result["facts"] and not result["images"]
    assert result["answer"] == workflow_module.LOW_GROUNDING_MESSAGE
    assert not any(name in {workflow_module.SSE_EVENT_SOURCES, workflow_module.SSE_EVENT_FACTS,
        workflow_module.SSE_EVENT_IMAGES} for name, _ in events)


def test_successful_empty_scoped_sql_reports_absent_confirmed_unit_price(monkeypatch):
    result, events, _ = _run(monkeypatch, sql_mode="spec", query="Giá căn CH-13A bao nhiêu?")
    assert result["answer"] == workflow_module.NO_CONFIRMED_UNIT_PRICE.format(unit_code="CH-13A")
    # A proven negative is still a blocked turn: LOW, reviewed, and no evidence
    # frame, because the completion that produced it publishes nothing.
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert not result["sources"] and not result["facts"] and not result["images"]
    assert not any(name in {workflow_module.SSE_EVENT_SOURCES, workflow_module.SSE_EVENT_FACTS,
        workflow_module.SSE_EVENT_IMAGES} for name, _ in events)


def test_failed_sql_lookup_reports_uncertain_not_absent(monkeypatch):
    result, _, _ = _run(monkeypatch, sql_mode="spec", sql_error=True,
        query="Giá căn CH-13A bao nhiêu?")
    # A lookup that never completed cannot support a claim of absence.
    assert result["answer"] == workflow_module.PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_verifier_outage_blocks_high_stakes_to_safe_fallback(monkeypatch):
    """Verifier down + high_stakes => BLOCK: safe fallback, no evidence frames."""
    result, events, _ = _run(monkeypatch, high_stakes=True, guard_fail="exception")
    assert result["answer"] == workflow_module.VERIFICATION_FALLBACK_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert not result["sources"] and not result["facts"]
    assert not any(name in {workflow_module.SSE_EVENT_SOURCES, workflow_module.SSE_EVENT_FACTS,
        workflow_module.SSE_EVENT_IMAGES} for name, _ in events)


def test_verifier_outage_warns_review_only_for_number_free_low_stakes(monkeypatch):
    """Opposite control: verifier down + low-stakes number-free + evidence => WARN publish."""
    result, events, _ = _run(monkeypatch, guard_fail="exception")
    assert result["answer"] == CLAIM
    assert result["requires_review"] is True
    assert result["sources"]
