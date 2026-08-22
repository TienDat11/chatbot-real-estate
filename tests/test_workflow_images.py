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

from api.workflow import RagQueryWorkflow

from api import workflow as workflow_module
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.rewrite import RoutedResult


class _FakeReranker:
    """No-op reranker: returns the chunks it is given (the app-side rerank call)."""

    async def rerank(self, query, chunks):
        return chunks


def _make_routed(rewritten: str) -> RoutedResult:
    """A rag-only routed result (needs_sql/geo False) so the legs short-circuit."""
    return RoutedResult(
        rewritten=rewritten,
        routing={
            "needs_rag": False,
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
        yield "câu trả lời"

    async def fake_output_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="MEDIUM", requires_review=False, verdicts={})

    async def fake_audit(entry):
        return None

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", fake_stream)
    monkeypatch.setattr(workflow_module, "guard_output", fake_output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)

    return events


def _run_workflow(
    raw_query: str, rewritten: str, images: list[dict], monkeypatch
) -> tuple[dict, dict, list]:
    """Run the whole workflow with the shared patch set; return (result, calls, events)."""
    calls = {"search_query": None}
    events = _patch_workflow(monkeypatch, rewritten=rewritten)

    async def fake_search(query):
        calls["search_query"] = query
        return images

    monkeypatch.setattr(workflow_module, "search_images", fake_search)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(query=raw_query, session_id=None, history=[])

    result = asyncio.run(go())
    return result, calls, events


def test_merge_calls_search_images_with_rewritten_query(monkeypatch):
    """The merge step must search by the REWRITTEN query (self-contained), not the
    raw user input — the caption embedding only matches against a full phrase."""
    raw_query = "căn CH-3 giá bao nhiêu"
    rewritten = "mặt bằng căn hộ CH-03 giá bán hiện tại"
    result, calls, _ = _run_workflow(
        raw_query, rewritten, [{"image_id": "i1", "score": 0.9}], monkeypatch
    )

    assert calls["search_query"] == rewritten
    assert calls["search_query"] != raw_query  # raw input never used
    assert result["images"] == [{"image_id": "i1", "score": 0.9}]


def test_merge_emits_sse_images_event_with_search_results(monkeypatch):
    """SSE_EVENT_IMAGES must fire exactly once with the full list search_images
    returned — the payload contract the FE gallery renders from."""
    images = [{"image_id": "i1", "score": 0.9}, {"image_id": "i2", "score": 0.8}]
    result, calls, events = _run_workflow(
        "giá căn CH-3", "giá bán căn hộ CH-03", images, monkeypatch
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
    result, _, events = _run_workflow("giá căn CH-3", "giá bán căn hộ CH-03", [], monkeypatch)

    name = workflow_module.SSE_EVENT_IMAGES
    image_events = [(e, d) for e, d in events if e == name]
    assert len(image_events) == 1
    assert image_events[0][1] == {"images": []}
    assert result["images"] == []
    assert result["answer"] == "câu trả lời"  # pipeline completed, answer intact
