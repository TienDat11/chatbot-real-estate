"""Pipeline wiring tests for the illustrative-image enrichment step.

Locks the merge-step contract introduced with the image-search fix:

- merge calls ``search_images`` with the REWRITTEN query, never the raw input;
- the SSE images event carries exactly the list ``search_images`` returned;
- ``StopEvent.result["images"]`` mirrors the stored list, and an empty/degraded
  search result still lets the pipeline finish with a normal answer (images are a
  garnish, never a reason to fail the pipeline).

Harness mirrors tests/test_workflow_stamp.py (drive the real RagQueryWorkflow with
every external call monkeypatched — no LLM, no DB, no network). One difference:
``merge`` uses ``ctx.collect_events``, which only works inside a running step, so
the step cannot be invoked directly the way ``sql_leg`` is in test_workflow_stamp
— the whole workflow is run through ``wf.run()`` instead.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
import api.application.services.merge as merge_module
from api.application.services.generate import build_messages
from api.application.services.rag_leg import RagLegResult
from api.workflow import RagQueryWorkflow
from api.application.services.project_context import ProjectContext
from api import workflow as workflow_module
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.rewrite import RoutedResult


_ACTIVE_CATALOGUE = (("camellia", "Camellia"),)


def _project_context(project_key="camellia") -> ProjectContext:
    return ProjectContext(project_key, "normal", _ACTIVE_CATALOGUE)


# One registry-authorized chunk: the merge step only emits a row the document
# registry signs off on, and image publication rides the same evidence gate.
_CONTENT = "Camellia có vườn cây xanh và lối dạo bộ."


def _chunk() -> dict:
    return {"id": "camellia-amenities", "content": _CONTENT, "score": 0.91}


def _record() -> dict:
    return {
        "chunk_id": "camellia-amenities",
        "registry_content": _CONTENT,
        "doc_id": "camellia-doc",
        "title": "Hồ sơ tiện ích Camellia",
        "section": "Tiện ích",
        "kind": "project",
        "project_key": "camellia",
        "status": "published",
        "effective_from": date.today() - timedelta(days=1),
        "effective_to": None,
    }


class _FakeReranker:
    """No-op reranker: returns the chunks it is given (the app-side rerank call)."""

    async def rerank(self, query, chunks):
        return chunks


def _make_routed(rewritten: str) -> RoutedResult:
    """A rag-only routed result (needs_sql/geo False) so those legs short-circuit;
    needs_rag stays True so the merge step has authority to publish images."""
    return RoutedResult(
        rewritten=rewritten,
        routing={
            "needs_rag": True,
            "needs_sql": False,
            "structured_path": "none",
            "needs_geo": False,
        },
        sql_spec=None,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=False,
        as_of=None,
    )


def _patch_workflow(monkeypatch, *, rewritten: str) -> list[tuple[str, dict]]:
    """Patch every external call the 8-step workflow touches EXCEPT search_images
    (the caller installs a capturing fake for it).

    Returns the ``events`` list the on_event callback appends to, so the caller
    can assert on the SSE payloads.
    """
    events: list[tuple[str, dict]] = []

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed(rewritten)

    async def fake_stream(merged, history, high_stakes):
        # The real stream_answer builds the prompt through build_messages, which
        # records the rows output_guard verifies before it may publish anything.
        # The harness must drive that same seam or images have no authority.
        build_messages(merged, history)
        yield "câu trả lời"

    async def fake_rag(*args, **kwargs):
        return RagLegResult(chunks=[_chunk()])

    class _Connection:
        async def fetch(self, sql_text, ids):
            return [_record()]

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *exc):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_pool():
        return _Pool()

    async def fake_output_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="MEDIUM", requires_review=False, verdicts={})

    async def fake_audit(entry):
        return None

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "run_rag_leg", fake_rag)
    monkeypatch.setattr(workflow_module, "stream_answer", fake_stream)
    monkeypatch.setattr(workflow_module, "guard_output", fake_output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    monkeypatch.setattr(merge_module, "get_ro_pool", fake_pool)

    return events


def _run_workflow(
    raw_query: str, rewritten: str, images: list[dict], monkeypatch, project_key="camellia"
) -> tuple[dict, dict, list]:
    """Run the whole workflow with the shared patch set; return (result, calls, events)."""
    calls = {"search_query": None}
    events = _patch_workflow(monkeypatch, rewritten=rewritten)

    async def fake_search(query, project_key=None):
        calls["search_query"] = query
        return images
    monkeypatch.setattr(workflow_module, "search_images", fake_search)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(
            query=raw_query, session_id=None, history=[], project_context=_project_context(project_key)
        )

    result = asyncio.run(go())
    return result, calls, events


def test_merge_calls_search_images_with_rewritten_query(monkeypatch):
    """The merge step must search by the REWRITTEN query (self-contained), not the
    raw user input — the caption embedding only matches against a full phrase."""
    # The raw turn must be non-price: the P0-03C publication gate clears the
    # evidence/image payloads for an unanswered price question, so a price raw
    # query would never reach the image-publication path this test pins.
    raw_query = "căn CH-3 có tiện ích gì"
    rewritten = "mặt bằng căn hộ CH-03 giá bán hiện tại"
    result, calls, _ = _run_workflow(
        raw_query,
        rewritten,
        [{"image_id": "i1", "score": 0.9, "project_key": "camellia", "unit_code": "CH-03"}],
        monkeypatch,
    )
    assert calls["search_query"] == rewritten
    assert calls["search_query"] != raw_query  # raw input never used
    assert result["images"] == [
        {"image_id": "i1", "score": 0.9, "project_key": "camellia", "unit_code": "CH-03"}
    ]


def test_merge_emits_sse_images_event_with_search_results(monkeypatch):
    """SSE_EVENT_IMAGES must fire exactly once with the full list search_images
    returned — the payload contract the FE gallery renders from."""
    images = [
        {
            "image_id": "i1",
            "score": 0.9,
            "project_key": "camellia",
            "unit_code": "CH-03",
        },
        {
            "image_id": "i2",
            "score": 0.8,
            "project_key": "camellia",
            "unit_code": "CH-03",
        },
    ]
    result, calls, events = _run_workflow(
        "căn CH-3 có tiện ích gì", "giá bán căn hộ CH-03", images, monkeypatch
    )

    name = workflow_module.SSE_EVENT_IMAGES
    image_events = [(e, d) for e, d in events if e == name]
    assert len(image_events) == 1
    assert image_events[0][1] == {"images": images}
    assert result["images"] == images
    assert calls["search_query"] == "giá bán căn hộ CH-03"


def test_merge_empty_images_still_completes_pipeline(monkeypatch):
    """search_images returning [] must not crash the pipeline: the images SSE
    event carries {"images": []}, StopEvent.result["images"] is [], and the
    answer still streams through normally."""
    result, _, events = _run_workflow("căn CH-3 có tiện ích gì", "giá bán căn hộ CH-03", [], monkeypatch)

    name = workflow_module.SSE_EVENT_IMAGES
    image_events = [(e, d) for e, d in events if e == name]
    assert len(image_events) == 1
    assert image_events[0][1] == {"images": []}
    assert result["images"] == []
    # The empty search must not suppress the answer: the pipeline still streams
    # and returns the normal answer text.
    assert result["answer"] == "câu trả lời"


# ==============================================================================
# Full price-board branch: deterministic fetch vs semantic search
# ==============================================================================

# "bảng giá từng loại căn hộ" needs EVERY board page — the semantic top_k cap
# truncates exactly when completeness matters, so a matching intent plus a known
# project_key switches the merge step to the deterministic fetch seam.


def _run_image_branch(
    monkeypatch,
    *,
    rewritten: str,
    project_key: str | None,
    intent: bool,
    board: list[dict],
    semantic_images: list[dict],
) -> tuple[dict, dict, list]:
    """Run the real workflow with both image seams instrumented."""
    calls = {"intent_query": None, "fetch_project": None, "search": None}
    events = _patch_workflow(monkeypatch, rewritten=rewritten)

    def fake_intent(text):
        calls["intent_query"] = text
        return intent

    async def fake_fetch(key):
        calls["fetch_project"] = key
        return board

    async def fake_search(*args, **kwargs):
        calls["search"] = (args, kwargs)
        return semantic_images

    monkeypatch.setattr(workflow_module, "is_full_price_board_query", fake_intent)
    monkeypatch.setattr(workflow_module, "fetch_price_board_images", fake_fetch)
    monkeypatch.setattr(workflow_module, "search_images", fake_search)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(
            # Non-price and unit-code-free: the raw turn must publish (P0-03C
            # clears images on an unanswered price question), and a concrete
            # unit code would make the presentation filter drop the un-keyed
            # semantic fixture. The board intent is mocked, so the price-board
            # rewritten text still exercises the fetch-vs-search wiring.
            query="tiện ích dự án camellia có gì",
            session_id=None,
            history=[],
            project_context=_project_context(project_key or "camellia"),
        )

    result = asyncio.run(go())
    return result, calls, events


def test_full_board_intent_uses_deterministic_fetch(monkeypatch):
    """Intent match + project_key -> ALL board pages via the deterministic seam;
    the semantic search (top_k-capped) is never called."""
    board = [{"image_id": f"bg-{p}", "match": "semantic"} for p in range(1, 7)]
    result, calls, events = _run_image_branch(
        monkeypatch,
        rewritten="bảng giá từng loại căn hộ camellia",
        project_key="camellia",
        intent=True,
        board=board,
        semantic_images=[{"image_id": "should-not-appear"}],
    )

    assert calls["intent_query"] == "bảng giá từng loại căn hộ camellia"
    assert calls["fetch_project"] == "camellia"
    assert calls["search"] is None
    assert result["images"] == board
    name = workflow_module.SSE_EVENT_IMAGES
    image_events = [(e, d) for e, d in events if e == name]
    assert len(image_events) == 1
    assert image_events[0][1] == {"images": board}


def test_single_type_intent_keeps_semantic_path(monkeypatch):
    """No full-board intent -> the existing semantic path is untouched (the
    scoped call shape rides along), and the fetch seam stays idle."""
    semantic_images = [{"image_id": "sem", "score": 0.9}]
    result, calls, _ = _run_image_branch(
        monkeypatch,
        rewritten="giá bán căn hộ CH-03 hiện tại",
        project_key="camellia",
        intent=False,
        board=[{"image_id": "should-not-appear"}],
        semantic_images=semantic_images,
    )

    assert calls["search"] == (("giá bán căn hộ CH-03 hiện tại",), {"project_key": "camellia"})
    assert calls["fetch_project"] is None
    assert result["images"] == semantic_images


def test_full_board_intent_requires_project_context(monkeypatch):
    workflow = RagQueryWorkflow()

    async def run_without_project_context():
        await workflow.run(query="bảng giá từng loại căn hộ", session_id=None, history=[])

    with pytest.raises(ValueError, match="project_context is required"):
        asyncio.run(run_without_project_context())
