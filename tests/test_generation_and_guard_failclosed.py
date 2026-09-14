"""Fail-closed contract for answer generation and output verification (W1-01/W1-02).

Two defects this locks down:

* ``generate.stream_answer`` used to swallow a provider failure, YIELD an
  apology string as if it were the answer, and still mark the answer complete
  (``finally``), so guard_output verified that text and the FE rendered it as
  content. A failure is now a typed exception that reaches the transport, which
  turns it into a retryable ``error`` frame.
* ``workflow.output_guard`` mapped a guard timeout to MEDIUM/no-review, so the
  one answer nobody managed to check shipped with the *best* non-LOW verdict.
  Verification being unavailable now forces LOW, which is what raises
  ``requires_review`` for the human loop.

Harness mirrors tests/test_workflow_images.py: the real RagQueryWorkflow runs
with every external call monkeypatched (no LLM, no DB, no network).
"""

from __future__ import annotations

import asyncio

import pytest
from api.workflow import RagQueryWorkflow

from api import workflow as workflow_module
from api.application.services import generate as generate_module
from api.application.services.generate import (
    GenerationProviderError,
    GenerationTimeout,
    stream_answer,
)
from api.application.services.merge import Merged
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.guard_output import guard_output
from api.domain.services.rewrite import RoutedResult

# --------------------------------------------------------------------------
# stream_answer: typed failure instead of a fake textual answer
# --------------------------------------------------------------------------


def _merged() -> Merged:
    return Merged(rag_blocks="", evidence_blocks="", sources=[], facts=[], meta={})


class _FakeLlm:
    """LLM adapter whose stream behaviour is supplied per test."""

    def __init__(self, *, tokens=(), exc=None, delay=0.0):
        self._tokens = tokens
        self._exc = exc
        self._delay = delay

    async def _gen(self):
        for token in self._tokens:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield token
        if self._exc is not None:
            raise self._exc

    def stream(self, messages, model=None, max_tokens=None):
        return self._gen()


def _install_llm(monkeypatch, llm) -> None:
    monkeypatch.setattr(generate_module, "llm", llm)


def test_provider_error_raises_typed_error_and_no_fake_answer(monkeypatch):
    _install_llm(monkeypatch, _FakeLlm(exc=RuntimeError("provider 503")))
    merged = _merged()

    async def collect():
        return [t async for t in stream_answer(merged, [], False)]

    with pytest.raises(GenerationProviderError):
        asyncio.run(collect())
    # The old contract marked this failed turn as a finished answer.
    assert merged.meta.get("answer_complete") is not True


def test_timeout_raises_typed_timeout_and_no_fake_answer(monkeypatch):
    _install_llm(monkeypatch, _FakeLlm(tokens=("xin",), delay=0.4))
    monkeypatch.setattr(generate_module.settings, "llm_timeout_s", 0.05)
    merged = _merged()

    async def collect():
        return [t async for t in stream_answer(merged, [], False)]

    with pytest.raises(GenerationTimeout):
        asyncio.run(collect())
    assert merged.meta.get("answer_complete") is not True


def test_normal_completion_still_marks_answer_complete(monkeypatch):
    _install_llm(monkeypatch, _FakeLlm(tokens=("câu ", "trả lời")))
    merged = _merged()

    async def collect():
        return [t async for t in stream_answer(merged, [], False)]

    assert asyncio.run(collect()) == ["câu ", "trả lời"]
    assert merged.meta["answer_complete"] is True


# --------------------------------------------------------------------------
# Workflow: generation failure propagates, guard failure fails closed
# --------------------------------------------------------------------------


class _FakeReranker:
    async def rerank(self, query, chunks):
        return chunks


def _make_routed(rewritten: str) -> RoutedResult:
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


def _patch_workflow(monkeypatch, *, stream, output_guard):
    events: list[tuple[str, dict]] = []
    audits: list[dict] = []

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed("giá căn CH-03")

    async def fake_audit(entry):
        audits.append(entry)
        return None

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    return events, audits


def test_generate_step_propagates_generation_failure(monkeypatch):
    """A provider failure must never surface as token content: no apology string
    reaches the SSE token frame, and the pipeline raises for the transport to
    answer with its retryable error frame."""

    async def failing_stream(merged, history, high_stakes):
        raise GenerationProviderError("provider 503")
        yield  # pragma: no cover — generator marker only

    async def output_guard(answer, facts, sources, routing, meta=None):
        raise AssertionError("output_guard must not run on a failed generation")

    events, audits = _patch_workflow(
        monkeypatch, stream=failing_stream, output_guard=output_guard
    )

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(query="giá căn CH-03", session_id=None, history=[])

    with pytest.raises(GenerationProviderError):
        asyncio.run(go())
    assert [e for e, _ in events if e == "token"] == []
    assert audits == []


def test_output_guard_timeout_fails_closed(monkeypatch):
    """Verification unavailable => LOW + requires_review, never MEDIUM/no-review."""

    async def ok_stream(merged, history, high_stakes):
        yield "câu trả lời"

    async def slow_output_guard(answer, facts, sources, routing, meta=None):
        await asyncio.sleep(0.3)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    _patch_workflow(monkeypatch, stream=ok_stream, output_guard=slow_output_guard)
    monkeypatch.setitem(workflow_module.STEP_TIMEOUTS, "output_guard", 0.01)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: None)
        return await wf.run(query="giá căn CH-03", session_id=None, history=[])

    result = asyncio.run(go())
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_output_guard_exception_fails_closed(monkeypatch):
    """A verifier that raises is also 'unverified', not 'passed'."""

    async def ok_stream(merged, history, high_stakes):
        yield "câu trả lời"

    async def broken_output_guard(answer, facts, sources, routing, meta=None):
        raise RuntimeError("verifier crashed: SELECT price FROM units")

    _, audits = _patch_workflow(
        monkeypatch, stream=ok_stream, output_guard=broken_output_guard
    )

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: None)
        return await wf.run(query="giá căn CH-03", session_id=None, history=[])

    result = asyncio.run(go())
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    # Stable identifiers only: the verifier's message (which can carry query or
    # SQL fragments) must not reach the audit trail.
    dumped = repr(audits)
    assert "RuntimeError" in dumped
    assert "verifier crashed" not in dumped


# --------------------------------------------------------------------------
# guard_output: a citation to a non-existent evidence id is a grounding failure
# --------------------------------------------------------------------------


def test_orphan_citation_forces_low_and_review():
    facts = [{"fe_id": "fe-001", "fields": {"price": 2_000_000_000}}]
    sources = [{"doc_id": "d1", "title": "Bảng giá"}]
    result = asyncio.run(
        guard_output(
            "Giá 2 tỷ [fe-999]",
            facts,
            sources,
            {},
            {"sql_row_count": 3, "strong_chunks": 2},
        )
    )
    assert result.verdicts["citation_grounding"]["status"] == "fail"
    assert result.confidence == "LOW"
    assert result.requires_review is True


def test_grounded_citation_keeps_high_confidence():
    """The fail-closed rule must not drag down a properly grounded answer."""
    facts = [{"fe_id": "fe-001", "fields": {"price": 2_000_000_000}}]
    sources = [{"doc_id": "d1", "title": "Bảng giá"}]
    result = asyncio.run(
        guard_output(
            "Giá 2 tỷ [fe-001] theo Bảng giá",
            facts,
            sources,
            {},
            {"sql_row_count": 3, "strong_chunks": 2},
        )
    )
    assert result.verdicts["citation_grounding"]["status"] == "pass"
    assert result.confidence == "HIGH"
