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
from datetime import date, timedelta
import pytest
from api.workflow import RagQueryWorkflow
from api.application.services.project_context import ProjectContext
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
from api.application.services import merge as merge_module
from api.application.services.rag_leg import RagLegResult

_ACTIVE_CATALOGUE = (("camellia", "Camellia"),)


def _project_context(project_key="camellia") -> ProjectContext:
    return ProjectContext(project_key, "normal", _ACTIVE_CATALOGUE)

# --------------------------------------------------------------------------
# stream_answer: typed failure instead of a fake textual answer
# --------------------------------------------------------------------------


def _merged() -> Merged:
    return Merged(rag_blocks="", evidence_blocks="", sources=[], facts=[], meta={})
def _scoped_evidence() -> Merged:
    """A real ``merge_context`` output: one selected, current, in-scope RAG row
    plus one confirmed fact. Built with the production builders so the prompt
    rows the gate re-reads are the ones production would serialize."""
    chunk = {
        "id": "chunk-1", "content": "Nguồn dự án", "score": 0.9,
        "doc_id": "d1", "title": "Nguồn dự án", "section": "Tổng quan",
        "project_key": "camellia", "status": "published",
        "effective_from": (date.today() - timedelta(days=1)).isoformat(),
        "effective_to": None,
    }
    evidence = [{
        "fe_id": "fe-001", "subject": "project:camellia", "policy_key": None,
        "fields": {"unit_count": 2}, "note": None,
        "effective_from": (date.today() - timedelta(days=1)).isoformat(),
        "effective_to": None, "quality": "exact", "trust_level": "confirmed",
    }]
    return Merged(
        rag_blocks=merge_module.build_rag_context([chunk]),
        evidence_blocks=merge_module.build_evidence_context(evidence),
        sources=merge_module.build_sources([chunk]),
        facts=merge_module.build_facts(evidence),
        meta={
            "project_key": "camellia", "retrieval_project_key": "camellia",
            "selected_chunk_ids": ("chunk-1",),
            "selected_project_keys": ("camellia",),
            "selected_fact_ids": (),
            "selected_fe_ids": ("fe-001",),
        },
    )


def _scoped_price_evidence(unit: str = "CH-03", price: int = 2_850_000_000):
    """Same as ``_scoped_evidence`` but with a FINANCE-authorized price row.

    P0-03C only lets a figure ship when a selected prompt row carries a fact id
    proven against the SQL leg plus a current published source document and an
    active same-project campaign. The plain ``_scoped_evidence`` row has none
    of those, so a money figure bound to it is a limitation, not a publication.

    Returns ``(merged, sql_rows)``: the workflow re-reads the leg output as
    ``meta["_fact_evidence"]``, so a financial fixture must supply BOTH halves.
    """
    start = (date.today() - timedelta(days=1)).isoformat()
    chunk = {
        "id": "chunk-1", "content": "Nguồn dự án", "score": 0.9,
        "doc_id": "d1", "title": "Bảng giá", "section": "Tổng quan",
        "project_key": "camellia", "status": "published",
        "effective_from": start, "effective_to": None,
    }
    lineage = {
        "unit": "vnd",
        "source_doc_id": "d1",
        "source_doc_status": "published",
        "source_doc_project_key": "camellia",
        "source_doc_effective_from": start,
        "source_doc_effective_to": None,
        "campaign_key": "cam-2026q3",
        "campaign_project_key": "camellia",
        "campaign_status": "active",
        "campaign_effective_from": start,
        "campaign_effective_to": None,
        "campaign_source_doc_id": "d1",
        "campaign_source_doc_status": "published",
        "campaign_source_doc_project_key": "camellia",
        "campaign_source_doc_effective_from": start,
        "campaign_source_doc_effective_to": None,
    }
    evidence = [{
        "fe_id": "fe-001", "fact_id": 203, "subject": f"unit:camellia/{unit}",
        "policy_key": "htls", "fields": {"price_vnd": price}, "note": None,
        "effective_from": start, "effective_to": None,
        "quality": "exact", "trust_level": "confirmed",
        **lineage,
    }]
    return (
        Merged(
            rag_blocks=merge_module.build_rag_context([chunk]),
            evidence_blocks=merge_module.build_evidence_context(evidence),
            sources=merge_module.build_sources([chunk]),
            facts=merge_module.build_facts(evidence),
            meta={
                "project_key": "camellia", "retrieval_project_key": "camellia",
                "selected_chunk_ids": ("chunk-1",),
                "selected_project_keys": ("camellia",),
                "selected_fact_ids": (203,),
                "selected_fe_ids": ("fe-001",),
            },
        ),
        evidence,
    )


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

def _recording(stream):
    """Wrap a stream stub so the generate step's real side effect still happens:
    ``build_messages`` records ``_prompt_rag_rows`` / ``_prompt_fact_rows`` via
    ``_record_prompt_evidence`` (``generate.py:93-99,120,149``). The publication
    gate reads only those keys, so a bare stub would exercise a state
    production can never reach."""
    async def run(merged, history, high_stakes):
        generate_module.build_messages(merged, history)
        async for token in stream(merged, history, high_stakes):
            yield token

    return run


def _run_wf(
    monkeypatch,
    *,
    project_context,
    stream,
    output_guard,
    output_guard_timeout=None,
    log=None,
    audits=None,
    merged_result=None,
    query=None,
    rag_chunks=None,
    registry_record=None,
    sql_rows=None,
):
    """Run the real workflow with patched external services and scoped fixtures.

    ``sql_rows`` is the real SQL leg's output: P0-03C re-reads it as
    ``meta["_fact_evidence"]`` and authorizes a figure only when the selected
    prompt row is byte-identical to the leg row it was serialized from, so a
    financial fixture must supply the leg, not just the prompt projection.
    """
    events: list[tuple[str, dict]] = []

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        rewritten = clean if rag_chunks is not None else "thông tin căn hộ"
        routed = _make_routed(rewritten)
        if rag_chunks is not None:
            routed.routing["needs_rag"] = True
        if sql_rows is not None:
            routed.routing["needs_sql"] = True
            routed.routing["structured_path"] = "nl2sql"
            routed.sql_spec = {"source": "facts"}
        return routed

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
    monkeypatch.setattr(workflow_module, "stream_answer", _recording(stream))
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    if sql_rows is not None:
        async def fake_sql(*args):
            return workflow_module.SqlLegResult(
                [dict(row) for row in sql_rows], {"mode": "spec", "source": "facts"}
            )

        monkeypatch.setattr(workflow_module, "run_sql_leg", fake_sql)
    if rag_chunks is not None:
        async def fake_rag(*args):
            return RagLegResult(chunks=rag_chunks)

        class RegistryConnection:
            async def fetch(self, sql, ids):
                return [registry_record] if registry_record is not None else []

        class RegistryAcquire:
            async def __aenter__(self):
                return RegistryConnection()

            async def __aexit__(self, *args):
                return None

        class RegistryPool:
            def acquire(self):
                return RegistryAcquire()

        async def fake_pool():
            return RegistryPool()

        monkeypatch.setattr(workflow_module, "run_rag_leg", fake_rag)
        monkeypatch.setattr(merge_module, "get_ro_pool", fake_pool)
        monkeypatch.setattr(workflow_module, "search_images", lambda *args, **kwargs: asyncio.sleep(0, result=[]))
    if merged_result is not None:
        async def fake_merge(query, rag_chunks, evidence, as_of, project_context):
            return merged_result

        monkeypatch.setattr(workflow_module, "merge_context", fake_merge)
    if output_guard_timeout is not None:
        monkeypatch.setitem(
            workflow_module.STEP_TIMEOUTS, "output_guard", output_guard_timeout
        )

    async def go():
        wf = RagQueryWorkflow(on_event=_cb)
        return await wf.run(
            query=query if query is not None else "thông tin căn hộ",
            session_id=None, history=[], project_context=project_context
        )
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
        return await wf.run(query="giá căn CH-03", session_id=None, history=[], project_context=_project_context())

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
        project_context=_project_context(),
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
        monkeypatch, stream=ok_stream, output_guard=broken_output_guard, audits=audits,
        project_context=_project_context(),
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
        monkeypatch,
        stream=stream,
        output_guard=output_guard,
        log=log,
        project_context=_project_context(),
        merged_result=_scoped_evidence(),
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
        project_context=_project_context(),
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
        monkeypatch, stream=stream, output_guard=broken_guard, audits=audits,
        project_context=_project_context(),
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
        return await wf.run(query="giá căn CH-03", session_id=None, history=[], project_context=_project_context())

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
    # Number-free on purpose: this test owns the TRANSPORT contract (delivered
    # exactly once, token == done), so the figure it carries would only add an
    # unverified money claim on top. Priced publication is covered by
    # tests/test_financial_claim_publication.py against lineage-backed rows.
    draft_answer = "căn CH-03 thuộc khu dân trí trung tâm [fe-001] theo Bảng giá"
    # P0-05: the internal [fe-001] marker is now canonicalized BEFORE the
    # final check/hash, so the delivered string is the sanitized one.
    full_answer = "căn CH-03 thuộc khu dân trí trung tâm theo Bảng giá"
    chunks = ["căn CH-03 ", "thuộc khu ", "dân trí trung tâm ", "[fe-001] ", "theo ", "Bảng giá"]

    async def stream(merged, history, high_stakes):
        for chunk in chunks:
            yield chunk

    received: list[str] = []

    async def output_guard(answer, facts, sources, routing, meta=None):
        received.append(answer)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    price_merged, price_rows = _scoped_price_evidence()
    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=output_guard,
        project_context=_project_context(),
        merged_result=price_merged,
        sql_rows=price_rows,
    )

    assert len(received) == 1
    assert received[0] == draft_answer
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    # Exactly one token frame carrying the full verified answer.
    assert token_text == full_answer
    assert result["answer"] == token_text
    assert result["answer"] == full_answer


def test_low_confidence_with_same_project_evidence_publishes_reviewable_answer(monkeypatch):
    """A completed LOW review verdict remains useful when scope is verified."""
    original = "Thông tin căn hộ đang được chuyên viên kiểm tra thêm."

    async def stream(merged, history, high_stakes):
        yield original

    async def low_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(
            confidence="LOW", requires_review=True, verdicts={"quality": "low"}
        )

    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=low_guard,
        project_context=_project_context(),
        merged_result=_scoped_evidence(),
    )

    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert token_text == original
def test_low_numeric_failure_returns_scoped_uncertainty_not_unsupported_answer(monkeypatch):
    original = "Căn CH-03 có giá 99 tỷ theo bảng giá."

    async def stream(merged, history, high_stakes):
        yield original

    audits: list[dict] = []
    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=guard_output,
        project_context=_project_context(),
        merged_result=_scoped_evidence(),
        audits=audits,
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )

    assert original not in token_text
    assert original not in result["answer"]
    assert result["answer"] == workflow_module.LOW_GROUNDING_MESSAGE
    assert audits[-1]["guard_verdicts"]["numeric_grounding"] == "fail"


def test_low_orphan_citation_returns_scoped_uncertainty(monkeypatch):
    original = "Thông tin chi tiết [fe-999] đã được ghi nhận."

    async def stream(merged, history, high_stakes):
        yield original

    async def low_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(
            confidence="LOW",
            requires_review=True,
            verdicts={"numeric_grounding": "pass", "citation_grounding": {"status": "fail"}},
        )

    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=low_guard,
        project_context=_project_context(),
        merged_result=_scoped_evidence(),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )

    assert original not in token_text
    assert original not in result["answer"]
    assert result["answer"] == workflow_module.LOW_GROUNDING_MESSAGE


def test_low_with_no_evidence_remains_fail_closed(monkeypatch):
    original = "Nội dung chưa có nguồn xác nhận."

    async def stream(merged, history, high_stakes):
        yield original

    async def low_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="LOW", requires_review=True, verdicts={})

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=low_guard, project_context=_project_context()
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )

    assert original not in token_text
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE


def test_low_cross_project_evidence_remains_fail_closed(monkeypatch):
    original = "Thông tin đã có nguồn."

    async def stream(merged, history, high_stakes):
        yield original

    async def low_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="LOW", requires_review=True, verdicts={})

    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=low_guard,
        project_context=_project_context(),
        merged_result=Merged(
            rag_blocks="",
            evidence_blocks="",
            sources=[{"doc_id": "d1", "title": "Nguồn", "project_key": "other-project"}],
            facts=[],
            meta={"project_key": "camellia", "retrieval_project_key": "camellia"},
        ),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )

    assert original not in token_text
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE


def test_low_with_sources_but_no_selected_provenance_remains_fail_closed(monkeypatch):
    """Sources are present but carry no selected provenance, so the prompt rows
    cannot match a selection and the gate must fail closed.

    The unresolved-scope branch this test used to name is dead on a real run:
    ``validate_project_key`` rejects a blank project_key, so ``ProjectContext``
    can never be built with one and ``retrieval_project`` is always a non-empty
    str — ``unresolved_scope`` never fires. The reachable block reason here is a
    grounding failure, which the copy rule maps to LOW_GROUNDING_MESSAGE.
    """
    original = "Thông tin đã có nguồn."
    async def stream(merged, history, high_stakes):
        yield original
    async def low_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="LOW", requires_review=True, verdicts={})
    unprovenanced_evidence = Merged(
        rag_blocks="",
        evidence_blocks="",
        sources=[{"doc_id": "d1", "title": "Nguồn"}],
        facts=[],
        meta={},
    )
    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=low_guard,
        project_context=_project_context(),
        merged_result=unprovenanced_evidence,
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert result["answer"] == workflow_module.LOW_GROUNDING_MESSAGE


def test_low_without_completed_verifier_remains_fail_closed(monkeypatch):
    original = "Thông tin đã có nguồn."

    async def stream(merged, history, high_stakes):
        yield original

    async def broken_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(confidence="LOW", requires_review=True, verdicts={})

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=broken_guard, output_guard_timeout=0.001,
        project_context=_project_context(),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )

    assert original not in token_text
    assert result["answer"] == VERIFICATION_FALLBACK_MESSAGE




def test_medium_success_publishes_original_once(monkeypatch):
    """MEDIUM confidence with requires_review (high-stakes) still publishes the
    verified answer exactly once. A LOW verdict is not an automatic veto — the
    decision is whether selected, current, same-project evidence exists."""
    # Number-free for the same reason as test_case_e: this test owns the
    # MEDIUM-publishes-once tier contract, not figure authority.
    draft_answer = "căn CH-03 thuộc khu dân trí trung tâm [fe-001] theo Bảng giá"
    full_answer = "căn CH-03 thuộc khu dân trí trung tâm theo Bảng giá"
    chunks = ["căn CH-03 ", "thuộc khu ", "dân trí trung tâm ", "[fe-001] ", "theo ", "Bảng giá"]

    async def stream(merged, history, high_stakes):
        for chunk in chunks:
            yield chunk

    received: list[str] = []

    async def output_guard(answer, facts, sources, routing, meta=None):
        received.append(answer)
        return OutputGuardResult(
            confidence="MEDIUM", requires_review=True, verdicts={"high_stakes": True}
        )

    price_merged, price_rows = _scoped_price_evidence()
    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=output_guard,
        project_context=_project_context(),
        merged_result=price_merged,
        sql_rows=price_rows,
    )

    assert len(received) == 1
    assert received[0] == draft_answer
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
        await wf.run(query="giá căn CH-03", session_id="s_d1", history=[], project_context=_project_context())

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

    The turn is a price-availability question and the verifier is unavailable,
    so the copy is the failed-price one: an unchecked lookup is uncertainty,
    never a claim that no price exists.

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
    result = await wf.run(query="thông tin giá căn CH-03", session_id="s_d2", history=[], project_context=_project_context())
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    for forbidden in ("diện tích", "68 m2", "giá 2 tỷ", "[fe-001]"):
        assert forbidden not in token_text
    assert token_text == workflow_module.PRICE_LOOKUP_UNCERTAIN
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    for forbidden in ("diện tích", "68 m2", "giá 2 tỷ", "[fe-001]"):
        assert forbidden not in result["answer"]
    assert result["answer"] == workflow_module.PRICE_LOOKUP_UNCERTAIN


# --------------------------------------------------------------------------
# guard_output: a citation to a non-existent evidence id is a grounding failure
# --------------------------------------------------------------------------


def test_orphan_citation_forces_low_and_review():
    facts = [{"fe_id": "fe-001", "fields": {"price_vnd": 2_000_000_000}, "unit": "vnd"}]
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
    facts = [{"fe_id": "fe-001", "fields": {"price_vnd": 2_000_000_000}, "unit": "vnd"}]
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

    audits: list[dict] = []
    result, events = _run_wf(
        monkeypatch,
        stream=stream,
        output_guard=guard_output,
        audits=audits,
        project_context=_project_context(),
        merged_result=Merged(
            rag_blocks="",
            evidence_blocks="",
            sources=[{"doc_id": "d1", "title": "Bảng giá"}],
            facts=[{"fe_id": "fe-001", "fields": {"price_vnd": 2_000_000_000}, "unit": "vnd"}],
            meta={"project_key": "camellia", "retrieval_project_key": "camellia"},
        ),
    )


    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert token_text == workflow_module.LOW_GROUNDING_MESSAGE
    assert original not in result["answer"]
    assert result["answer"] == workflow_module.LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True

    # Non-vacuity: the audit must show the citation check failing ALONE — the
    # figure is grounded by fe-001, so numeric_grounding must read "pass".
    guard_verdicts = audits[-1]["guard_verdicts"]
    assert guard_verdicts["numeric_grounding"] == "pass"
    assert guard_verdicts["orphan_numbers"] == []
    assert guard_verdicts["citation_grounding"]["status"] == "fail"
    assert guard_verdicts["citation_grounding"]["missing"] == ["fe-999"]


def test_camellia_amenity_rag_area_publishes_without_sql_facts(monkeypatch):
    """A selected, current project document can ground its own amenity area only."""
    from datetime import date, timedelta
    source = "Hồ sơ tiện ích Camellia"

    content = (
        "Tiện ích dự án Camellia: nha_tre diện tích 434.2 m2; "
        "shcd_tang2 157.4 m2; be_boi 381.6 m2; gym 134.3 m2; "
        "yoga 43.6 m2; shcd_tang3 232.1 m2."
    )
    chunk = {"id": "camellia-amenities", "content": content, "score": 0.91}
    today = date.today()
    record = {
        "chunk_id": chunk["id"], "registry_content": content, "doc_id": "camellia-doc",
        "title": source, "section": "Tiện ích", "kind": "project",
        "project_key": "camellia", "status": "published",
        "effective_from": today - timedelta(days=1),
        "effective_to": today + timedelta(days=1),
    }
    approved = f"Nhà trẻ rộng 434,2 m² theo {source}."
    cases = [
        (approved, record, True),
        (f"Nhà trẻ rộng 434.2 m² theo {source}.", record, True),
        (f"Nhà trẻ rộng 434,2 m². Theo {source}.", record, False),
        (f"Nhà trẻ rộng 434,2 m². Giá căn 99 tỷ theo {source}.", record, False),
        (f"Nhà trẻ giá 99 tỷ theo {source}.", record, False),
        (f"Nhà trẻ rộng 434,3 m² theo {source}.", record, False),
        (f"Gym rộng 434,2 m² theo {source}.", record, False),
        (approved, {**record, "project_key": "soleil"}, False),
        (approved, {**record, "effective_to": today - timedelta(days=1)}, False),
    ]
    for answer, selected_record, allowed in cases:
        with monkeypatch.context() as scope:
            async def stream(merged, history, high_stakes):
                yield answer

            audits = []
            result, events = _run_wf(
                scope, stream=stream, output_guard=guard_output,
                query="Dự án camellia tiện ích gì?", project_context=_project_context(),
                rag_chunks=[chunk], registry_record=selected_record, audits=audits,
            )
            token_text = "".join(
                data.get("text", "")
                for event, data in events if event == workflow_module.SSE_EVENT_TOKEN
            )
            verdict = audits[-1]["guard_verdicts"]["numeric_grounding"]
            if allowed:
                assert verdict == "pass"
                assert token_text == result["answer"] == answer
            else:
                assert token_text != answer
                assert result["answer"] != answer
                assert verdict == "fail"
def test_partial_generation_has_no_evidence_or_answer_publication(monkeypatch):
    partial = "Camellia có tiện ích bí mật."

    async def stream(merged, history, high_stakes):
        yield partial
        raise GenerationProviderError("provider reset")

    async def output_guard(*args, **kwargs):
        raise AssertionError("guard must not run after partial generation")

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed(clean)

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", stream)
    monkeypatch.setattr(workflow_module, "guard_output", output_guard)

    events = []
    async def go():
        return await RagQueryWorkflow(on_event=lambda event, data: events.append((event, data))).run(
            query="Camellia tiện ích", session_id=None, history=[], project_context=_project_context())

    with pytest.raises(GenerationProviderError):
        asyncio.run(go())
    assert not any(event in {workflow_module.SSE_EVENT_TOKEN, workflow_module.SSE_EVENT_SOURCES,
        workflow_module.SSE_EVENT_FACTS, workflow_module.SSE_EVENT_IMAGES} for event, _ in events)


# --------------------------------------------------------------------------
# P0-03C: the citation "pending" gate, and the verifier-timeout pair
# --------------------------------------------------------------------------


def test_pending_citation_publishes_only_when_the_answer_cites_nothing(monkeypatch):
    """``pending`` means the verifier could not check the citations at all.

    An answer that carries no ``[fe-xxx]`` has nothing unverified to hide, so it
    may ship — flagged for review. An answer that DOES cite is exactly the case
    pending was raised for, so it blocks.
    """
    async def stream(merged, history, high_stakes):
        yield "Dự án có vườn cây xanh và lối dạo bộ."

    async def pending_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(
            confidence="MEDIUM",
            requires_review=True,
            verdicts={"citation_grounding": {"status": "pending"}},
        )

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=pending_guard,
        project_context=_project_context(), merged_result=_scoped_evidence(),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert token_text == "Dự án có vườn cây xanh và lối dạo bộ."
    assert result["answer"] == token_text
    assert result["requires_review"] is True


def test_pending_citation_blocks_an_answer_that_cites_an_id(monkeypatch):
    original = "Dự án có vườn cây xanh [fe-001]."

    async def stream(merged, history, high_stakes):
        yield original

    async def pending_guard(answer, facts, sources, routing, meta=None):
        return OutputGuardResult(
            confidence="MEDIUM",
            requires_review=True,
            verdicts={"citation_grounding": {"status": "pending"}},
        )

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=pending_guard,
        project_context=_project_context(), merged_result=_scoped_evidence(),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert original not in token_text
    assert result["answer"] == workflow_module.LOW_GROUNDING_MESSAGE
    assert result["confidence"] == "LOW"


def test_verifier_timeout_publishes_a_safe_descriptive_but_blocks_a_number_bearing_one(
    monkeypatch,
):
    """The WARN/BLOCK split is scoped, not global.

    A number-free draft that verification could not check still says nothing the
    selected evidence does not support, so it ships reviewed. A draft carrying a
    figure nobody could check does not.
    """
    async def slow_guard(answer, facts, sources, routing, meta=None):
        await asyncio.sleep(0.3)
        return OutputGuardResult(confidence="HIGH", requires_review=False, verdicts={})

    async def descriptive_stream(merged, history, high_stakes):
        yield "Dự án có vườn cây xanh và lối dạo bộ."

    result, events = _run_wf(
        monkeypatch, stream=descriptive_stream, output_guard=slow_guard,
        output_guard_timeout=0.01,
        project_context=_project_context(), merged_result=_scoped_evidence(),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert token_text == "Dự án có vườn cây xanh và lối dạo bộ."
    assert result["answer"] == token_text
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True

    async def numbered_stream(merged, history, high_stakes):
        yield "Dự án có 2 tỷ vốn quy mô đầu tư."

    result, events = _run_wf(
        monkeypatch, stream=numbered_stream, output_guard=slow_guard,
        output_guard_timeout=0.01,
        project_context=_project_context(), merged_result=_scoped_evidence(),
    )
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert "2 tỷ" not in token_text
    assert result["answer"] == workflow_module.VERIFICATION_FALLBACK_MESSAGE
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True


def test_real_verifier_pending_publishes_an_uncited_answer(monkeypatch):
    """The `pending` branch, proven with the REAL verifier rather than a stub.

    guard_output reaches `pending` when the answer carries no ``[fe-xxx]`` span
    at all: there is nothing to check, so it neither passes nor fails. That is
    exactly the case the publish-without-id rule governs, and a canned verdict
    would only prove the workflow's arithmetic, not that the branch is reachable.
    """
    answer = "Dự án có vườn cây xanh và lối dạo bộ."
    audits: list[dict] = []

    async def stream(merged, history, high_stakes):
        yield answer

    result, events = _run_wf(
        monkeypatch, stream=stream, output_guard=guard_output, audits=audits,
        project_context=_project_context(), merged_result=_scoped_evidence(),
    )
    # Non-vacuity: the real verifier really did land on `pending`.
    assert audits[-1]["guard_verdicts"]["citation_grounding"]["status"] == "pending"
    token_text = "".join(
        d.get("text", "") for e, d in events if e == workflow_module.SSE_EVENT_TOKEN
    )
    assert token_text == answer
    assert result["answer"] == answer
    # Nothing was verified, so it ships flagged rather than clean.
    assert result["requires_review"] is True
