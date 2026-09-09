"""Regression tests for the two user-reported pipeline bugs.

Bug A — streamed answer truncated mid-word: the StreamingSanitizer hold-back
tail was never flushed (zero callers) and an abnormal inner-run exit produced a
done frame without answer, so the FE kept partial streamed text. These tests
pin: flush on every exit path, stream/done equivalence with one-shot sanitize,
and the degraded done payload carrying the sanitized partial.

Bug B — bot denies price data that exists (Camellia treo board): empty
FACT_EVIDENCE must be hydrated from v_unit_estimates for price/unit questions,
with estimate/range semantics so guard confidence lands at MEDIUM, never LOW.
"""

from __future__ import annotations

import inspect
import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from api.workflow import RagQueryWorkflow
from fastapi.testclient import TestClient

from api import workflow as workflow_module
from api.application.pipelines.conv_workflow import RagRgreConvWorkflow
from api.application.pipelines.workflow import QueryRejected
from api.application.services.estimates_fallback import (
    apply_estimates_fallback,
    build_estimate_evidence,
)
from api.application.services.merge import Merged
from api.application.services.output_sanitizer import sanitize_answer_text
from api.application.services.query_quota_gate import UnmanagedTurnContext
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.guard_output import guard_output
from api.domain.services.rewrite import RoutedResult

# A realistic answer whose internal-id bracket is split across deltas and whose
# Vietnamese tail would previously sit in the hold-back buffer forever.
_FULL_ANSWER = (
    "Dự án em đang có giá treo hiện tại cho studio ạ [doc_camellia_gia_2026q3] "
    "Mã căn CH-09, CH-12A giá từ 1,98 - 2,64 tỷ. Anh/chị để lại số điện thoại "
    "hoặc email để em gửi bảng giá chi tiết nhé."
)


class _OfflineQuotaGate:
    """Test-only quota seam; production keeps the fail-closed gate unchanged."""

    async def prepare_turn(self, **kwargs):
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        return None


@pytest.fixture(autouse=True)
def _offline_quota_gate(monkeypatch):
    """Keep regression tests offline without changing production fail-closed behavior."""
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: _OfflineQuotaGate(),
    )


def _split_deltas(text: str, size: int = 7) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class _StreamStubInner:
    """Fake inner workflow: streams deltas through the conv wrapper, then either
    returns a canned result or dies with an exception (mid-stream abort)."""

    def __init__(self, wf_ref, deltas, result=None, error=None):
        self._wf = wf_ref
        self._deltas = deltas
        self._result = result
        self._error = error

    def run(self, **kwargs):
        outer = self

        async def handler():
            for d in outer._deltas:
                res = outer._wf._emit_sanitized_stream("token", {"text": d})
                if inspect.isawaitable(res):
                    await res
            if outer._error is not None:
                raise outer._error
            return dict(outer._result or {})

        return handler()


def _collect_events():
    events: list[tuple[str, dict]] = []

    def on_event(event: str, data: dict) -> None:
        events.append((event, data))

    return events, on_event


# ==============================================================================
# Bug A — hold-back tail survives abnormal exits
# ==============================================================================


@pytest.mark.asyncio
async def test_stream_dying_mid_tail_loses_nothing_vs_one_shot():
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    wf._inner = _StreamStubInner(
        wf, _split_deltas(_FULL_ANSWER), error=RuntimeError("llm gateway dropped")
    )
    result = await wf.run(query="giá studio", session_id="s_tail_abort", history=[])
    expected = sanitize_answer_text(_FULL_ANSWER)
    # done.answer equals one-shot sanitize of the full text (nothing lost).
    assert result["answer"] == expected
    assert result["answer_truncated"] is True
    assert result["confidence"] == "LOW"
    assert result["requires_review"] is True
    # Emitted token frames concatenated equal the same text (stream complete).
    streamed = "".join(d.get("text", "") for e, d in events if e == "token")
    assert streamed == expected
    # No internal chunk-id leak even when the bracket was split mid-delta.
    assert "doc_camellia" not in streamed and "doc_camellia" not in result["answer"]


@pytest.mark.asyncio
async def test_normal_completion_stream_equals_done_and_one_shot():
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    wf._inner = _StreamStubInner(
        wf, _split_deltas(_FULL_ANSWER), result={"answer": _FULL_ANSWER, "requires_review": False}
    )
    result = await wf.run(query="giá studio", session_id="s_tail_ok", history=[])
    expected = sanitize_answer_text(_FULL_ANSWER)
    assert result["answer"] == expected
    assert "answer_truncated" not in result
    streamed = "".join(d.get("text", "") for e, d in events if e == "token")
    assert streamed == expected


@pytest.mark.asyncio
async def test_query_rejected_keeps_error_contract():
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    wf._inner = _StreamStubInner(wf, [], error=QueryRejected("L1 rejected"))
    with pytest.raises(QueryRejected):
        await wf.run(query="spam", session_id="s_tail_reject", history=[])


@pytest.mark.asyncio
async def test_inner_fail_without_streamed_tokens_reraises():
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    wf._inner = _StreamStubInner(wf, [], error=RuntimeError("guard blew up"))
    with pytest.raises(RuntimeError):
        await wf.run(query="giá studio", session_id="s_tail_nostr", history=[])


@pytest.mark.asyncio
async def test_rerun_after_abort_gets_fresh_state():
    # A rerun on the same instance must not inherit the previous tail/flag.
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    failing = _StreamStubInner(wf, _split_deltas(_FULL_ANSWER), error=RuntimeError("x"))
    wf._inner = failing
    first = await wf.run(query="giá", session_id="s_rerun", history=[])
    assert first["answer_truncated"] is True
    ok_result = {"answer": "Căn studio giá treo 1,98 tỷ anh nhé.", "requires_review": False}
    wf._inner = _StreamStubInner(wf, _split_deltas(ok_result["answer"]), result=ok_result)
    second = await wf.run(query="giá", session_id="s_rerun", history=[])
    assert "answer_truncated" not in second
    assert second["answer"] == "Căn studio giá treo 1,98 tỷ anh nhé."


@pytest.mark.asyncio
async def test_stream_tail_flushed_on_timeout_degraded_done():
    # A workflow-level timeout surfaces as TimeoutError from the inner run;
    # the Bug A contract is the same as any mid-stream death: flush + degrade.
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    wf._inner = _StreamStubInner(
        wf, _split_deltas(_FULL_ANSWER), error=TimeoutError("step budget hit")
    )
    result = await wf.run(query="giá studio", session_id="s_tail_timeout", history=[])
    expected = sanitize_answer_text(_FULL_ANSWER, terminated=True)
    assert result["answer"] == expected
    assert result["answer_truncated"] is True
    streamed = "".join(d.get("text", "") for e, d in events if e == "token")
    assert streamed == expected


@pytest.mark.asyncio
async def test_done_answer_neutralizes_unterminated_marker():
    # The authoritative done answer is re-sanitized with the termination pass:
    # a marker that reached the result payload cut-off still never reaches the FE.
    events, on_event = _collect_events()
    wf = RagRgreConvWorkflow(on_event=on_event)
    raw = "Giá studio 1,98 tỷ [chunk_a1b2c3d4 chưa đóng"
    wf._inner = _StreamStubInner(
        wf, _split_deltas(raw), result={"answer": raw, "requires_review": False}
    )
    result = await wf.run(query="giá studio", session_id="s_done_term", history=[])
    assert result["answer"] == "Giá studio 1,98 tỷ "
    assert "[chunk_a1b2c3d4" not in result["answer"]
    assert "".join(d.get("text", "") for e, d in events if e == "token") == result["answer"]


def test_asgi_sse_done_replaces_partial_after_midstream_death(monkeypatch):
    """Full ASGI repro of Bug A over POST /query SSE: done.answer arrives
    complete (no mid-word cut) even though the inner run died mid-tail."""
    from api.interfaces.api.main import create_app

    class FailingPipe:
        async def run(
            self, query, session_id, as_of, history, project_key=None, device_id=None, on_event=None
        ):
            wf = RagRgreConvWorkflow(on_event=on_event)
            wf._inner = _StreamStubInner(
                wf, _split_deltas(_FULL_ANSWER), error=RuntimeError("gateway cut")
            )
            return await wf.run(
                query=query,
                session_id=session_id,
                as_of=as_of,
                history=history or [],
                project_key=project_key,
                device_id=device_id,
            )

    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr("api.application.pipelines.conv_workflow.RagQueryPipelineConv", FailingPipe)
    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", _fake_resolve)
    client = TestClient(create_app())
    resp = client.post(
        "/query",
        headers={"Accept": "text/event-stream"},
        json={"query": "Lấy cho anh căn rẻ nhất. Và studio vip nhất", "project_key": "camellia"},
    )
    assert resp.status_code == 200
    frames: list[tuple[str, dict]] = []
    for block in resp.text.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    names = [e for e, _ in frames]
    assert names[-1] == "done"
    done = frames[-1][1]
    expected = sanitize_answer_text(_FULL_ANSWER)
    # (a) done.answer is the COMPLETE sentence — no mid-word cut.
    assert done.get("answer") == expected
    assert done["answer"].endswith("nhé.")
    # Honest truncation flag present for FE surfacing.
    assert done.get("answer_truncated") is True
    # Streamed tokens + done agree; no chunk-id leak anywhere on the wire.
    streamed = "".join(d.get("text", "") for e, d in frames if e == "token")
    assert streamed == expected


def test_asgi_sse_normal_completion_done_answer_equals_streamed_text(monkeypatch):
    """Normal-completion contract over POST /query SSE: the done frame's
    ``answer`` equals the concatenated token stream (the same text, not empty),
    so a client that only reads ``done`` gets the full answer."""
    from api.interfaces.api.main import create_app

    class OkPipe:
        async def run(
            self, query, session_id, as_of, history, project_key=None, device_id=None, on_event=None
        ):
            wf = RagRgreConvWorkflow(on_event=on_event)
            wf._inner = _StreamStubInner(
                wf,
                _split_deltas(_FULL_ANSWER),
                result={"answer": _FULL_ANSWER, "requires_review": False},
            )
            return await wf.run(
                query=query,
                session_id=session_id,
                as_of=as_of,
                history=history or [],
                project_key=project_key,
                device_id=device_id,
            )

    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr("api.application.pipelines.conv_workflow.RagQueryPipelineConv", OkPipe)
    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", _fake_resolve)
    client = TestClient(create_app())
    resp = client.post(
        "/query",
        headers={"Accept": "text/event-stream"},
        json={"query": "Lấy cho anh căn rẻ nhất. Và studio vip nhất", "project_key": "camellia"},
    )
    assert resp.status_code == 200
    frames: list[tuple[str, dict]] = []
    for block in resp.text.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines.get("event"), json.loads(lines.get("data", "{}"))))
    names = [e for e, _ in frames]
    assert names[-1] == "done"
    done = frames[-1][1]
    expected = sanitize_answer_text(_FULL_ANSWER)
    # done.answer is the SAME text the token stream produced (never empty).
    assert done.get("answer") == expected
    streamed = "".join(d.get("text", "") for e, d in frames if e == "token")
    assert done["answer"] == streamed
    assert "answer_truncated" not in done
    # No chunk-id leak anywhere on the wire.
    assert "doc_camellia" not in resp.text


# ==============================================================================
# Bug B — indicative treo board hydrates empty FACT_EVIDENCE
# ==============================================================================


def _estimate_rows():
    return [
        {
            "subject_key": "unit:camellia/studio",
            "display_name": "Căn hộ Studio",
            "project_key": "camellia",
            "policy_key": "htls",
            "price_min_vnd": Decimal("1.98E+9"),
            "price_max_vnd": Decimal("2.64E+9"),
            "deposit_pct": Decimal("30.00"),
            "term_months": Decimal("18"),
            "interest_rate_pct": Decimal("0"),
        },
        {
            "subject_key": "unit:camellia/studio",
            "display_name": "Căn hộ Studio",
            "project_key": "camellia",
            "policy_key": "som95",
            "price_min_vnd": Decimal("1.72E+9"),
            "price_max_vnd": Decimal("2.30E+9"),
            "deposit_pct": None,
            "term_months": None,
            "interest_rate_pct": None,
        },
        {
            "subject_key": "unit:camellia/3pn",
            "display_name": "Căn hộ 3PN",
            "project_key": "camellia",
            "policy_key": "htls",
            "price_min_vnd": Decimal("7.5E+9"),
            "price_max_vnd": Decimal("8.99E+9"),
            "deposit_pct": None,
            "term_months": None,
            "interest_rate_pct": None,
        },
        {  # unpriced row must be skipped, never fabricated as 0
            "subject_key": "unit:camellia/no-price",
            "display_name": "?",
            "project_key": "camellia",
            "policy_key": None,
            "price_min_vnd": None,
            "price_max_vnd": None,
            "deposit_pct": None,
            "term_months": None,
            "interest_rate_pct": None,
        },
    ]


def test_build_estimate_evidence_cheapest_first_and_shaped():
    evidence = build_estimate_evidence(_estimate_rows())
    assert len(evidence) == 2  # unpriced type skipped entirely
    assert [e["fe_id"] for e in evidence] == ["fe-001", "fe-002"]
    assert evidence[0]["subject"] == "unit:camellia/studio"
    # One block per TYPE: bounds aggregate across payment policies.
    assert evidence[0]["fields"]["price_min_vnd"] == 1_720_000_000
    assert evidence[0]["fields"]["price_max_vnd"] == 2_640_000_000
    assert evidence[0]["quality"] == "range"
    assert evidence[0]["trust_level"] == "estimate"
    assert "giá treo" in evidence[0]["note"]
    # Per-policy breakdown stays readable AND every policy bound is grounded as
    # a flat field so any figure generation quotes byte-matches FACT_EVIDENCE.
    assert evidence[0]["fields"]["policy_price_ranges"]["som95"] == "1720000000-2300000000"
    assert evidence[0]["fields"]["price_min_som95"] == 1_720_000_000
    assert all("v_unit_estimates" not in e["note"] for e in evidence)


def _estimate_rows_full_board():
    """The real Camellia board shape: 6 unit types x 4 payment policies = 24 rows."""
    board = {
        "unit:camellia/studio": (
            "Căn hộ Studio",
            [
                (1900000000, 2530000000),
                (1980000000, 2640000000),
                (1940000000, 2590000000),
                (1720000000, 2300000000),
            ],
        ),
        "unit:camellia/1p1": (
            "Căn hộ 1.5PN",
            [
                (3150000000, 3950000000),
                (3280000000, 4110000000),
                (3210000000, 4030000000),
                (2850000000, 3580000000),
            ],
        ),
        "unit:camellia/2pn-noi-khu": (
            "Căn hộ 2PN view nội khu",
            [
                (3740000000, 4790000000),
                (3900000000, 4990000000),
                (3820000000, 4890000000),
                (3390000000, 4340000000),
            ],
        ),
        "unit:camellia/2pn-mat-duong": (
            "Căn hộ 2PN mặt đường",
            [
                (4310000000, 5160000000),
                (4490000000, 5370000000),
                (4400000000, 5260000000),
                (3910000000, 4670000000),
            ],
        ),
        "unit:camellia/2pn-goc": (
            "Căn hộ 2PN góc",
            [
                (4440000000, 5750000000),
                (4630000000, 5990000000),
                (4540000000, 5870000000),
                (4030000000, 5210000000),
            ],
        ),
        "unit:camellia/3pn": (
            "Căn hộ 3PN góc view biển",
            [
                (7200000000, 8630000000),
                (7500000000, 8990000000),
                (7350000000, 8810000000),
                (6530000000, 7820000000),
            ],
        ),
    }
    policies = ["chuan", "htls", "thanh_thoi", "som95"]
    rows = []
    for subject_key, (display_name, ranges) in board.items():
        for policy_key, (lo, hi) in zip(policies, ranges):
            rows.append(
                {
                    "subject_key": subject_key,
                    "display_name": display_name,
                    "project_key": "camellia",
                    "policy_key": policy_key,
                    "price_min_vnd": lo,
                    "price_max_vnd": hi,
                    "deposit_pct": None,
                    "term_months": None,
                    "interest_rate_pct": None,
                }
            )
    return rows


def test_full_board_aggregates_all_six_types_within_cap():
    evidence = build_estimate_evidence(_estimate_rows_full_board())
    assert len(evidence) == 6  # cap applies to TYPES: nothing dropped at 8 rows
    assert [e["fe_id"] for e in evidence] == [f"fe-{i:03d}" for i in range(1, 7)]
    # Cheapest-first by aggregated min across the whole board.
    mins = [e["fields"]["price_min_vnd"] for e in evidence]
    assert mins == sorted(mins)
    studio = evidence[0]
    assert studio["subject"] == "unit:camellia/studio"
    assert studio["fields"]["price_min_vnd"] == 1_720_000_000
    assert studio["fields"]["price_max_vnd"] == 2_640_000_000
    threepn = evidence[-1]
    assert threepn["subject"] == "unit:camellia/3pn"
    assert threepn["fields"]["price_min_vnd"] == 6_530_000_000
    assert threepn["fields"]["price_max_vnd"] == 8_990_000_000
    display_names = {e["fields"]["display_name"] for e in evidence}
    assert display_names == {
        "Căn hộ Studio",
        "Căn hộ 1.5PN",
        "Căn hộ 2PN view nội khu",
        "Căn hộ 2PN mặt đường",
        "Căn hộ 2PN góc",
        "Căn hộ 3PN góc view biển",
    }
    # No internal view name may ride along into the LLM/UI payloads.
    assert all("v_unit_estimates" not in e["note"] for e in evidence)
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "v_unit_" not in serialized


@pytest.mark.asyncio
async def test_apply_fallback_mutates_merged_when_evidence_empty():
    merged = Merged(
        rag_blocks="",
        evidence_blocks="",
        sources=[],
        facts=[],
        meta={
            "project_key": "camellia",
            "query": "Lấy cho anh căn rẻ nhất. Và studio vip nhất",
            "rewritten": "Tìm căn hộ có giá rẻ nhất và studio cao cấp nhất",
        },
    )
    with patch(
        "api.application.services.estimates_fallback.fetch_unit_estimates",
        new=AsyncMock(return_value=_estimate_rows()),
    ):
        applied = await apply_estimates_fallback(merged)
    assert applied is True
    assert len(merged.facts) == 2
    assert merged.meta["estimates_fallback"] is True
    assert merged.meta["has_approx"] is True
    assert merged.meta["sql_row_count"] == 2
    assert "price_min_vnd" in merged.evidence_blocks


@pytest.mark.asyncio
async def test_apply_fallback_gates_non_price_turns():
    merged_legal = Merged(
        rag_blocks="",
        evidence_blocks="",
        sources=[],
        facts=[],
        meta={
            "project_key": "camellia",
            "query": "thế chấp cầm cố quy định thế nào",
            "rewritten": "quy định thế chấp",
        },
    )
    with patch(
        "api.application.services.estimates_fallback.fetch_unit_estimates",
        new=AsyncMock(return_value=_estimate_rows()),
    ) as fetch:
        applied = await apply_estimates_fallback(merged_legal)
    assert applied is False
    assert merged_legal.facts == []
    fetch.assert_not_called()

    merged_priced = Merged(
        rag_blocks="",
        evidence_blocks="",
        sources=[],
        facts=[{"fe_id": "fe-001"}],
        meta={"project_key": "camellia", "query": "căn rẻ nhất"},
    )
    assert await apply_estimates_fallback(merged_priced) is False

    merged_noproj = Merged(
        rag_blocks="",
        evidence_blocks="",
        sources=[],
        facts=[],
        meta={"query": "căn rẻ nhất"},
    )
    assert await apply_estimates_fallback(merged_noproj) is False


@pytest.mark.asyncio
async def test_guard_confidence_medium_with_estimate_evidence():
    evidence = build_estimate_evidence(_estimate_rows())
    # Post-aggregation contract: policy data lives INSIDE fields
    # (policy_price_ranges + per-policy bound fields), not as a top-level key.
    facts = [
        {"fe_id": e["fe_id"], "subject": e["subject"], "fields": e["fields"], "note": e["note"]}
        for e in evidence
    ]
    answer = (
        "Giá treo hiện tại cho studio là từ 1.980.000.000 đồng (1,98 tỷ) đến "
        "2.640.000.000 đồng (2,64 tỷ) theo phương thức trả chậm [fe-002], mã căn "
        "CH-09, CH-12A. Đây là giá định hướng, chưa phải bảng giá chính thức."
    )
    res = await guard_output(
        answer,
        facts,
        [{"title": "Giá định hướng Camellia Q3/2026"}],
        {"high_stakes": False},
        meta={"sql_row_count": 3, "strong_chunks": 0, "degraded": [], "has_approx": True},
    )
    verdicts = res.verdicts
    assert verdicts["numeric_grounding"] == "pass"
    assert res.confidence == "MEDIUM"  # estimate ranges cap HIGH, stay above LOW


# ==============================================================================
# Bug B wiring — merge stores the meta-populated merged object; generate's
# stream seam applies apply_estimates_fallback to that SAME object (pins that
# the fallback is live production code, not dead, and meta reaches it).
# ==============================================================================


class _FakeReranker:
    """No-op reranker: returns the chunks it is given (the app-side rerank call)."""

    async def rerank(self, query, chunks):
        return chunks


def _make_routed(rewritten: str) -> RoutedResult:
    """A rag-only routed result so the legs short-circuit in the wiring test."""
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


@pytest.mark.asyncio
async def test_workflow_seam_feeds_populated_meta_to_estimates_fallback(monkeypatch):
    """Production path for Bug B: workflow.merge writes meta.query/rewritten/
    project_key BEFORE store.set("merged"), then generate -> stream_answer runs
    apply_estimates_fallback(merged) on that same object, and the hydrated
    estimate facts reach the authoritative StopEvent payload."""
    events: list[tuple[str, dict]] = []
    captured: dict = {}

    async def fake_guard(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed("căn hộ có giá rẻ nhất dự án")

    async def fake_stream(merged, history, high_stakes):
        # The EXACT seam call generate.stream_answer performs before tier select.
        captured["meta"] = {k: merged.meta.get(k) for k in ("project_key", "query", "rewritten")}
        captured["applied"] = await apply_estimates_fallback(merged)
        yield "giá treo trả lời"

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

    with patch(
        "api.application.services.estimates_fallback.fetch_unit_estimates",
        new=AsyncMock(return_value=_estimate_rows()),
    ) as fetch:
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        result = await wf.run(
            query="Lấy cho anh căn rẻ nhất",
            session_id=None,
            history=[],
            project_key="camellia",
        )

    fetch.assert_awaited_once_with("camellia")
    assert captured["applied"] is True
    # Meta was populated by the merge step before generate consumed it.
    assert captured["meta"]["project_key"] == "camellia"
    assert captured["meta"]["query"] == "Lấy cho anh căn rẻ nhất"
    assert captured["meta"]["rewritten"]
    # Hydrated estimate evidence reached the output payload.
    assert [f["fe_id"] for f in result["facts"]] == ["fe-001", "fe-002"]
    assert result["confidence"] == "MEDIUM"
