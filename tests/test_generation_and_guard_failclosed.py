"""Fail-closed contract for answer generation and output verification (W1-01/W1-02).

Two defects this locks down:

* ``generate.stream_answer`` used to swallow a provider failure, YIELD an
  apology string as if it were the answer, and still mark the answer complete
  (``finally``), so guard_output verified that text and the FE rendered it as
  content. A failure is now a typed exception that reaches the transport,
  turning it into a retryable ``error`` frame.
* ``workflow.output_guard`` mapped a guard timeout to MEDIUM/no-review, so the
  one answer nobody managed to check shipped with the *best* non-LOW verdict.
  Verification being unavailable now forces LOW, raising ``requires_review``.

LGN-P0-002 extends this contract: no byte of an LLM-generated answer may reach
the customer (as a token frame OR inside the terminal done payload) until the
output guard verifies it. The ``generate`` step buffers every chunk in memory
without emitting tokens; ``output_guard`` emits the verified answer as ONE token
frame only on success, and on fail-closed (guard timeout/exception or LOW
verdict) emits NO original-answer token and returns the deterministic
safe-copy (VERIFICATION_FALLBACK_MESSAGE) in the done payload.

Harness mirrors tests/test_workflow_images.py: the real RagQueryWorkflow runs
with every external call monkeypatched (no LLM, no DB, no network).
"""

from __future__ import annotations

import asyncio

import pytest
from api.workflow import RagQueryWorkflow

from api import workflow as workflow_module
from api.application.pipelines.conv_workflow import RagRgreConvWorkflow
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
from api.workflow import VERIFICATION_FALLBACK_MESSAGE

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


def _run_wf(
    monkeypatch,
    *,
    stream,
    output_guard,
    output_guard_timeout=None,
    log=None,
    audits=None,
) -> tuple[dict, list[tuple[str, dict]]]:
    """Run the real RagQueryWorkflow with all externals patched.

    If ``log`` is provided (a shared list), token events are appended as
    ``("token", text)`` markers so ordering can be observed against guard
    markers appended by the fake ``output_guard``.

    If ``audits`` is provided (a shared list), each ``write_audit`` call appends
    the audit dict so fail-closed sanitization of exception metadata can be
    asserted.

    Returns ``(done_result, events)``. A generation/transport failure raises
    from within the run (see ``test_generate_step...`` for the expected case).
    """
    events: list[tuple[str, dict]] = []

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed("giá căn CH-03")

    async def fake_audit(entry):
        if audits is not None:
            audits.append(entry)
        return None

    def _cb(event: str, data: dict) -> None:
        events.append((event, data))
        if log is not None and event == workflow_module.SSE_EVENT_TOKEN:
            log.append(("token", data.get("text", "")))

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    if output_guard_timeout is not None:
        monkeypatch.setitem(
            workflow_module.STEP_TIMEOUTS, "output_guard", output_guard_timeout
        )

    async def go():
        wf = RagQueryWorkflow(on_event=_cb)
        return await wf.run(query="giá căn CH-03", session_id=None, history=[])

    return asyncio.run(go()), events


def test_generate_step_propagates_generation_failure(monkeypatch):
    """A provider failure must never surface as token content: no apology string
    reaches the SSE token frame, and the pipeline raises for the transport to
    answer with its retryable error frame."""

    async def failing_stream(merged, history, high_stakes):
        raise GenerationProviderError("provider 503")
        yield  # pragma: no cover — generator marker only

    async def output_guard(answer, facts, sources, routing, meta=None):
        raise AssertionError("output_guard must not run on a failed generation")

    audits: list[dict] = []
    events: list[tuple[str, dict]] = []

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
    monkeypatch.setattr(workflow_module, "stream_answer", failing_stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(query="giá căn CH-03", session_id=None, history=[])

    with pytest.raises(GenerationProviderError):
        asyncio.run(go())
    assert [e for e, _ in events if e == workflow_module.SSE_EVENT_TOKEN] == []
    assert audits == []



def test_output_guard_timeout_fails_closed(monkeypatch):
    """Verification unavailable => LOW + requires_review, never MEDIUM/no-review."""

    async def ok_stream(merged, history, high_stakes):
        yield "câu trả lời"

    async def slow_output_guard(answer, facts, sources, routing, meta=None):
        await asyncio.sleep(0.3)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    result, _events = _run_wf(
        monkeypatch,
        stream=ok_stream,
        output_guard=slow_output_guard,
        output_guard_timeout=0.01,
    )
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_output_guard_exception_fails_closed(monkeypatch):
    """A verifier that raises is also 'unverified', not 'passed'."""

    async def ok_stream(merged, history, high_stakes):
        yield "câu trả lời"

    async def broken_output_guard(answer, facts, sources, routing, meta=None):
        raise RuntimeError("verifier crashed: SELECT price FROM units")

    audits: list[dict] = []
    result, _events = _run_wf(
        monkeypatch, stream=ok_stream, output_guard=broken_output_guard, audits=audits
    )
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    # Stable identifiers only: the verifier's message (which can carry query or
    # SQL fragments) must not reach the audit trail.
    dumped = repr(audits)
    assert "RuntimeError" in dumped
    assert "verifier crashed" not in dumped


# --------------------------------------------------------------------------
# LGN-P0-002: verify-before-customer-delivery
# --------------------------------------------------------------------------


def test_case_a_no_token_before_guard_completion(monkeypatch):
    """Generation completes, the guard completes successfully, THEN token events.

    No SSE_EVENT_TOKEN may be emitted before the guard has verified the answer.
    The fake output_guard appends a ``guard_complete`` marker to the shared
    ``log``; the SSE callback appends ``token`` markers. Both share the same
    single-threaded event loop, so list-index order is transport-observable.
    """
    log: list[tuple[str, str]] = []
    verified: list[str] = []

    async def stream(merged, history, high_stakes):
        yield "diện tích "
        yield "68 m2"

    async def output_guard(answer, facts, sources, routing, meta=None):
        verified.append(answer)
        log.append(("guard_complete", ""))
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=output_guard, log=log
    )

    # The guard must have run on the success path.
    assert ("guard_complete", "") in log
    assert verified == ["diện tích 68 m2"]

    token_indices = [i for i, (kind, _) in enumerate(log) if kind == "token"]
    guard_idx = log.index(("guard_complete", ""))

    # Success path MUST emit exactly one verified token frame.
    assert len(token_indices) == 1, (
        f"expected exactly 1 token frame, got {len(token_indices)}: {log}"
    )
    # That token must appear strictly AFTER guard completion.
    token_idx = token_indices[0]
    assert guard_idx < token_idx, (
        f"token emitted before guard: token@{token_idx}, guard@{guard_idx}"
    )
    # The emitted token text equals the verified answer and the done payload.
    token_text = log[token_idx][1]
    assert token_text == "diện tích 68 m2"
    assert result["answer"] == token_text


def test_case_b_guard_timeout_original_not_emitted(monkeypatch):
    """Guard timeout => fail-closed; original answer absent from tokens and done."""
    original = "diện tích 68 m2, giá 2 tỷ [fe-001]"

    async def stream(merged, history, high_stakes):
        for chunk in ("diện tích ", "68 m2, ", "giá 2 tỷ [fe-001]"):
            yield chunk

    async def slow_guard(answer, facts, sources, routing, meta=None):
        await asyncio.sleep(0.5)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=slow_guard,
        output_guard_timeout=0.01,
    )

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert token_text == VERIFICATION_FALLBACK_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert original not in result["answer"]
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE


def test_case_c_guard_exception_original_not_emitted(monkeypatch):
    """Guard exception => original answer never emitted (token or done)."""
    original = "diện tích 68 m2, \\frac{a}{b} tỷ"

    async def stream(merged, history, high_stakes):
        for chunk in ("diện tích ", "68 m2, ", "\\frac{a}{b} tỷ"):
            yield chunk

    async def broken_guard(answer, facts, sources, routing, meta=None):
        raise RuntimeError("verifier crashed: SELECT price FROM units")

    audits: list[dict] = []
    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=broken_guard, audits=audits
    )

    # Verifier crash metadata is sanitized: stable type name only, no message.
    dumped = repr(audits)
    assert "RuntimeError" in dumped
    assert "verifier crashed" not in dumped

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert token_text == VERIFICATION_FALLBACK_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    assert original not in result["answer"]
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE

def test_case_d_provider_failure_no_partial_to_customer(monkeypatch):
    """Provider death after partial internal content must NOT leak a token or a
    done payload carrying raw partial text; the typed error propagates."""
    partial = "diện tích 68 m2"

    async def failing_stream(merged, history, high_stakes):
        yield "diện tích "
        yield "68 m2"
        raise GenerationProviderError("provider 503 stream reset")
        yield ""  # pragma: no cover — generator marker only

    async def output_guard(answer, facts, sources, routing, meta=None):
        raise AssertionError("output_guard must not run on a failed generation")

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
    monkeypatch.setattr(workflow_module, "stream_answer", failing_stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(query="giá căn CH-03", session_id=None, history=[])

    with pytest.raises(GenerationProviderError):
        asyncio.run(go())
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert partial not in token_text
    assert token_text == ""
    assert audits == []


def test_case_e_success_delivered_once_equals_verified(monkeypatch):
    """Success path: the answer is delivered exactly once — the joined token text
    equals the verified answer and the returned done payload matches."""
    full_answer = "diện tích 68 m2, giá 2.85 tỷ [fe-001] theo Bảng giá"
    chunks = ["diện tích ", "68 m2, ", "giá 2.85 tỷ ", "[fe-001] ", "theo ", "Bảng giá"]

    async def stream(merged, history, high_stakes):
        for chunk in chunks:
            yield chunk

    received: list[str] = []

    async def output_guard(answer, facts, sources, routing, meta=None):
        received.append(answer)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    result, events = _run_wf(monkeypatch, stream=stream, output_guard=output_guard)

    assert len(received) == 1
    assert received[0] == full_answer
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    # Exactly one token frame carrying the full verified answer.
    assert token_text == full_answer
    assert result["answer"] == token_text
    assert result["answer"] == full_answer


def test_low_confidence_does_not_publish_original(monkeypatch):
    """LGN-P0-002R: a guard verdict of LOW (e.g. grounding failure or high-stakes)
    is NOT safe to deliver — the original answer must be withheld and the
    deterministic safe-copy delivered exactly once as token == done.answer."""
    original = "căn CH-03 giá 2.85 tỷ [fe-001]"

    async def stream(merged, history, high_stakes):
        yield original

    async def low_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(
            confidence="LOW", requires_review=True, verdicts={"high_stakes": True}
        )

    result, events = _run_wf(monkeypatch, stream=stream, output_guard=low_guard)

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert token_text == VERIFICATION_FALLBACK_MESSAGE
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE
    assert original not in result["answer"]
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_medium_success_publishes_original_once(monkeypatch):
    """MEDIUM confidence with requires_review (high-stakes) still publishes the
    verified original answer exactly once — only LOW suppresses publication."""
    full_answer = "diện tích 68 m2, giá 2.85 tỷ [fe-001] theo Bảng giá"
    chunks = ["diện tích ", "68 m2, ", "giá 2.85 tỷ ", "[fe-001] ", "theo ", "Bảng giá"]

    async def stream(merged, history, high_stakes):
        for chunk in chunks:
            yield chunk

    received: list[str] = []

    async def output_guard(answer, facts, sources, routing, meta=None):
        received.append(answer)
        return OutputGuardResult(
            confidence="MEDIUM", requires_review=True, verdicts={"high_stakes": True}
        )

    result, events = _run_wf(monkeypatch, stream=stream, output_guard=output_guard)

    assert len(received) == 1
    assert received[0] == full_answer
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert token_text == full_answer
    assert result["answer"] == full_answer
    assert result["confidence"] == "MEDIUM"
    assert result["requires_review"] is True


# ---------------------------------------------------------------------------
# Conv wrapper: D1/D2 — real RagRgreConvWorkflow inner-fail paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case_d1_conv_failure_no_partial_leak(monkeypatch):
    """Conv wrapper: inner raises AFTER generation streamed partial content.

    Uses a real RagQueryWorkflow inner (stubbed legs, failing stream) inside the
    real RagRgreConvWorkflow. On baseline, generate emits tokens during streaming
    so the conv sanitizer releases prefixes; on failure the conv wrapper's
    ``_flush_stream_tail`` + partial-done synthesis leaks raw content. After the
    fix, generate buffers (no pre-guard tokens), so on failure the conv wrapper
    re-raises without any partial leak.

    Asserts: token frames contain NONE of the original answer.
    """
    original = "diện tích 68 m2, giá 2 tỷ [fe-001]"
    events: list[tuple[str, dict]] = []

    async def failing_stream(merged, history, high_stakes):
        yield "diện tích "
        yield "68 m2"
        raise GenerationProviderError("provider 503 stream reset")
        yield ""  # pragma: no cover — generator marker only

    async def output_guard(answer, facts, sources, routing, meta=None):
        raise AssertionError("output_guard must not run on a failed generation")

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed("giá căn CH-03")

    async def fake_audit(entry):
        return None

    def _cb(event: str, data: dict) -> None:
        events.append((event, data))

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", failing_stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)

    wf = RagRgreConvWorkflow(on_event=_cb)
    with pytest.raises(GenerationProviderError):
        await wf.run(query="giá căn CH-03", session_id="s_d1", history=[])

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert token_text == ""


@pytest.mark.asyncio
async def test_case_d2_conv_guard_timeout_no_partial_leak(monkeypatch):
    """Conv wrapper: guard timeout (inner completes, guard fails-closed).

    After the fix, output_guard emits the deterministic safe-copy as the sole
    token frame; the conv wrapper forwards that to the done payload, so the
    original is absent from token frames AND done payload.

    Asserts: original answer absent from token frames AND done payload;
    safe-copy delivered once and equals done.answer.
    """
    original = "diện tích 68 m2, giá 2 tỷ [fe-001]"
    events: list[tuple[str, dict]] = []

    async def ok_stream(merged, history, high_stakes):
        for chunk in ("diện tích ", "68 m2, ", "giá 2 tỷ [fe-001]"):
            yield chunk

    async def slow_guard(answer, facts, sources, routing, meta=None):
        await asyncio.sleep(0.5)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed("giá căn CH-03")

    async def fake_audit(entry):
        return None

    def _cb(event: str, data: dict) -> None:
        events.append((event, data))

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", ok_stream)
    monkeypatch.setattr(workflow_module, "guard_output", slow_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    monkeypatch.setitem(
        workflow_module.STEP_TIMEOUTS, "output_guard", 0.01
    )

    wf = RagRgreConvWorkflow(on_event=_cb)
    result = await wf.run(query="giá căn CH-03", session_id="s_d2", history=[])

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    for forbidden in ("diện tích", "68 m2", "giá 2 tỷ", "[fe-001]"):
        assert forbidden not in token_text
    assert token_text == VERIFICATION_FALLBACK_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    for forbidden in ("diện tích", "68 m2", "giá 2 tỷ", "[fe-001]"):
        assert forbidden not in result["answer"]
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE


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


def test_orphan_citation_workflow_fails_closed(monkeypatch):
    """LOW verdict from the REAL guard_output path (orphan citation) suppresses
    the original answer and delivers the safe-copy exactly once.

    The merge seam is pinned to evidence that DOES back the answer's "2 tỷ"
    (fe-001), so numeric_grounding passes and the only failing check is
    citation_grounding ([fe-999] names no known fe_id). That keeps the LOW
    verdict attributable to the citation path alone: a regression that breaks
    the citation check (or one that orphans the number) turns this red.
    """
    original = "Giá 2 tỷ [fe-999] theo Bảng giá"

    async def stream(merged, history, high_stakes):
        yield original

    async def fake_merge(query, rag_chunks, evidence, as_of, project_key=None):
        return Merged(
            rag_blocks="",
            evidence_blocks="",
            sources=[{"doc_id": "d1", "title": "Bảng giá"}],
            facts=[{"fe_id": "fe-001", "fields": {"price": 2_000_000_000}}],
        )

    monkeypatch.setattr(workflow_module, "merge_context", fake_merge)

    audits: list[dict] = []
    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=guard_output, audits=audits
    )

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert token_text == VERIFICATION_FALLBACK_MESSAGE
    assert original not in result["answer"]
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True

    # Non-vacuity: the audit must show the citation check failing ALONE — the
    # figure is grounded by fe-001, so numeric_grounding must read "pass".
    guard_verdicts = audits[-1]["guard_verdicts"]
    assert guard_verdicts["numeric_grounding"] == "pass"
    assert guard_verdicts["orphan_numbers"] == []
    assert guard_verdicts["citation_grounding"]["status"] == "fail"
    assert guard_verdicts["citation_grounding"]["missing"] == ["fe-999"]
