"""P0-03A selected evidence must be the only RAG publication authority."""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import pytest

import api.application.pipelines.workflow as workflow_module
from api.application.services import merge as merge_module
from api.application.services.merge import merge_context
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.rewrite import RoutedResult
from api.application.pipelines.workflow import RagQueryWorkflow

AS_OF = date(2026, 9, 29)
PROJECT = ProjectContext("camellia", "normal", (("camellia", "Camellia"),))
CONTENT = "Camellia có vườn cây xanh và lối dạo bộ." 
CLAIM = "Camellia có vườn cây xanh theo Hồ sơ tiện ích Camellia."


def _chunk(identifier="camellia-amenities", content=CONTENT):
    return {"id": identifier, "content": content, "score": 0.91}


def _record(chunk=None, **changes):
    chunk = chunk or _chunk()
    return {
        "chunk_id": chunk["id"], "registry_content": chunk["content"],
        "doc_id": "camellia-doc", "title": "Hồ sơ tiện ích Camellia",
        "section": "Tiện ích", "kind": "project", "project_key": "camellia",
        "status": "published", "effective_from": AS_OF - timedelta(days=1),
        "effective_to": None, **changes,
    }


async def _merge_with_registry(monkeypatch, chunks, rows=None, fail=None, evidence=None):
    async def pool():
        if fail:
            raise fail

        class Connection:
            async def fetch(self, sql, ids):
                if fail:
                    raise fail
                return rows or []

        class Acquire:
            async def __aenter__(self):
                return Connection()

            async def __aexit__(self, *args):
                return None

        class Pool:
            def acquire(self):
                return Acquire()

        return Pool()

    monkeypatch.setattr(merge_module, "get_ro_pool", pool)
    return await merge_context("Camellia tiện ích", chunks, evidence or [], AS_OF, "camellia")


def test_merge_serializes_only_matching_registry_selected_row(monkeypatch):
    selected = _chunk()
    merged = asyncio.run(_merge_with_registry(
        monkeypatch, [selected], [_record(selected)],
    ))
    payload = _serialized_rows(merged.rag_blocks)
    assert len(payload) == 1
    assert payload[0]["id"] == selected["id"]
    assert payload[0]["content"] == _record(selected)["registry_content"]
    assert merged.facts == []
    assert merged.meta["selected_chunk_ids"] == tuple(row["id"] for row in payload) == (selected["id"],)
    assert merged.meta["selected_project_keys"] == tuple(row["project_key"] for row in payload) == ("camellia",)
    assert merged.meta["selected_fact_ids"] == ()

class _Reranker:
    async def rerank(self, query, chunks):
        return chunks



def _run_workflow(monkeypatch, *, chunk, records=None, pool_error=None, draft=CLAIM, query="Camellia tiện ích"):
    events = []
    prompts = []
    async def input_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)
    async def rewrite(clean, history, as_of_iso):
        return RoutedResult(rewritten=clean, routing={"needs_rag": True, "needs_sql": False,
            "structured_path": "none", "needs_geo": False}, sql_spec=None,
            hl_keywords=[], ll_keywords=[], high_stakes=False, as_of=AS_OF)
    async def rag(*args):
        return RagLegResult(chunks=[chunk] if chunk else [])
    async def stream(merged, history, high_stakes):
        from api.application.services.generate import build_messages
        messages = build_messages(merged, history)
        prompt = next(message["content"] for message in reversed(messages) if message.get("role") == "user" and "RAG_CONTEXT (" in message.get("content", ""))
        rag_marker = "RAG_CONTEXT (chunks từ LightRAG, đã lọc hiệu lực + rerank):\n"
        fact_marker = "FACT_EVIDENCE ("
        merged.meta["_prompt_rag_rows"] = json.loads(prompt.split(rag_marker, 1)[1].split("\n", 1)[0])
        merged.meta["_prompt_fact_rows"] = json.loads(prompt.split(fact_marker, 1)[1].split("\n", 1)[1].split("\n", 1)[0])
        prompts.append((merged, prompt))
        yield draft
    async def guard(answer, observed_facts, sources, routing, meta=None):
        return OutputGuardResult(answer=answer, confidence="HIGH", requires_review=False, verdicts={})
    async def audit(entry): return None
    class Connection:
        async def fetch(self, sql, ids):
            if pool_error: raise pool_error
            return records or []
    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return None
    class Pool:
        def acquire(self): return Acquire()
    async def pool(): return Pool()
    monkeypatch.setattr(workflow_module, "guard_input", input_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _Reranker())
    monkeypatch.setattr(workflow_module, "run_rag_leg", rag)
    monkeypatch.setattr(workflow_module, "stream_answer", stream)
    monkeypatch.setattr(workflow_module, "guard_output", guard)
    monkeypatch.setattr(workflow_module, "write_audit", audit)
    monkeypatch.setattr(merge_module, "get_ro_pool", pool)
    monkeypatch.setattr(workflow_module, "search_images", lambda *a, **k: asyncio.sleep(0, result=[]))
    def callback(event, data): events.append((event, data))
    async def go():
        return await RagQueryWorkflow(on_event=callback).run(
            query=query, session_id=None, history=[], as_of=AS_OF.isoformat(), project_context=PROJECT)
    return asyncio.run(go()), events, prompts


def _serialized_rows(prompt):
    marker = "RAG_CONTEXT (chunks từ LightRAG, đã lọc hiệu lực + rerank):\n"
    return json.loads(prompt.split(marker, 1)[1].split("\n", 1)[0])

def _serialized_fact_rows(prompt):
    after_header = prompt.split("FACT_EVIDENCE (", 1)[1].split("\n", 1)[1]
    return json.loads(after_header.split("\n", 1)[0])



def test_matching_selected_row_serializes_selected_evidence(monkeypatch):
    chunk = _chunk()
    _, _, prompts = _run_workflow(monkeypatch, chunk=chunk, records=[_record(chunk)])
    merged, prompt = prompts[0]
    rows = _serialized_rows(prompt)
    assert len(rows) == 1 and rows[0]["id"] == chunk["id"]
    assert rows[0]["content"] == _record(chunk)["registry_content"]
    assert merged.facts == []
    assert merged.meta["selected_chunk_ids"] == tuple(row["id"] for row in rows) == (chunk["id"],)
    assert merged.meta["selected_project_keys"] == ("camellia",)
    assert merged.meta["selected_fact_ids"] == ()

def test_selected_fact_ids_match_serialized_fact_rows(monkeypatch):
    evidence = [
        {"fact_id": "fact-1", "fe_id": "fe-1", "subject": "unit:camellia/2pn-goc", "fields": {"price_vnd": 100}},
        {"fe_id": "fe-2", "subject": "unit:camellia/3pn", "fields": {"price_vnd": 200}},
    ]
    merged = asyncio.run(_merge_with_registry(monkeypatch, [], evidence=evidence))
    serialized = _serialized_fact_rows(merged.evidence_blocks)

    assert [row["fe_id"] for row in serialized] == ["fe-1", "fe-2"]
    assert [row.get("fact_id") for row in serialized if row.get("fact_id")] == ["fact-1"]
    assert merged.meta["selected_fe_ids"] == ("fe-1", "fe-2")
    assert merged.meta["selected_fact_ids"] == ("fact-1",)

    numeric = asyncio.run(_merge_with_registry(monkeypatch, [], evidence=[
        {"fact_id": 42, "fe_id": "fe-numeric", "subject": "unit:camellia/2pn", "fields": {"price_vnd": 100}},
    ]))
    assert numeric.meta["selected_fact_ids"] == (42,)
def test_model_content_mismatch_cannot_borrow_registry_authority(monkeypatch):
    raw = "Camellia có hồ bơi riêng miễn phí."
    chunk = _chunk(content=raw)
    _, _, prompts = _run_workflow(monkeypatch, chunk=chunk, records=[_record(_chunk())],
        draft="Camellia có hồ bơi riêng miễn phí theo Hồ sơ tiện ích Camellia.")
    merged, prompt = prompts[0]
    assert _serialized_rows(prompt) == []
    assert raw not in merged.rag_blocks
    assert not merged.meta["selected_chunk_ids"]
    assert not merged.meta["selected_project_keys"]
    assert not any(source.get("doc_id") == "camellia-doc" for source in merged.sources)
    assert not merged.meta["selected_fact_ids"]


@pytest.mark.parametrize("variant", [
    {"project_key": "soleil"}, {"status": "draft"},
    {"effective_from": AS_OF + timedelta(days=1)},
    {"effective_to": AS_OF}, {"registry_content": "   "},
    {"missing": True},
], ids=["foreign-project", "draft", "future", "expired-at-as-of",
       "blank-registry", "missing-row"])
def test_invalid_registry_has_no_selected_authority(monkeypatch, variant):
    chunk = _chunk()
    record = None if variant.get("missing") else _record(_chunk())
    if record:
        record.update({key: value for key, value in variant.items() if key != "missing"})
    _, _, prompts = _run_workflow(monkeypatch, chunk=chunk,
        records=[record] if record else [])
    merged, prompt = prompts[0]
    assert _serialized_rows(prompt) == []
    assert not merged.meta["selected_chunk_ids"]
    assert not merged.meta["selected_project_keys"]
    assert not any(source.get("doc_id") == "camellia-doc" for source in merged.sources)
    assert not merged.meta["selected_fact_ids"]


def test_matching_blank_candidate_and_registry_content_has_no_selected_authority(monkeypatch):
    chunk = _chunk(content="   ")
    record = _record(_chunk(content="   "))
    _, _, prompts = _run_workflow(monkeypatch, chunk=chunk, records=[record])
    merged, prompt = prompts[0]
    assert _serialized_rows(prompt) == []
    assert not merged.meta["selected_chunk_ids"]
    assert not merged.meta["selected_project_keys"]
    assert not any(source.get("doc_id") == "camellia-doc" for source in merged.sources)
    assert not merged.meta["selected_fact_ids"]


def test_nonblank_candidate_with_blank_registry_content_has_no_selected_authority(monkeypatch):
    chunk = _chunk()
    record = _record(_chunk(content="   "))
    _, _, prompts = _run_workflow(monkeypatch, chunk=chunk, records=[record])
    merged, prompt = prompts[0]
    assert _serialized_rows(prompt) == []
    assert not merged.meta["selected_chunk_ids"]
    assert not merged.meta["selected_project_keys"]
    assert not any(source.get("doc_id") == "camellia-doc" for source in merged.sources)
    assert not merged.meta["selected_fact_ids"]


def test_registry_failure_does_not_serialize_raw_reranked_content(monkeypatch):
    raw = "Camellia có sân thượng bí mật theo Hồ sơ tiện ích Camellia."
    _, _, prompts = _run_workflow(monkeypatch, chunk=_chunk(content=raw),
        pool_error=RuntimeError("registry unavailable"), draft=raw)
    merged, prompt = prompts[0]
    assert _serialized_rows(prompt) == []
    assert raw not in merged.rag_blocks
    assert not merged.meta["selected_chunk_ids"]
    assert not merged.meta["selected_project_keys"]
    assert not any(source.get("doc_id") == "camellia-amenities" for source in merged.sources)
    assert not merged.meta["selected_fact_ids"]


def test_empty_rag_response_serializes_no_selected_rows(monkeypatch):
    result, events, prompts = _run_workflow(monkeypatch, chunk=None, records=[], draft=CLAIM)
    merged, prompt = prompts[0]
    assert _serialized_rows(prompt) == []
    assert not merged.meta["selected_chunk_ids"]
    assert not merged.meta["selected_project_keys"]
    assert not merged.meta["selected_fact_ids"]
