"""Behavioral anchors for the sales/training prompt profiles + focused contracts.

Pins the DETAILED PROJECT-LOOKUP persona of api/prompts/training_policy.md v3
(specs, layouts, areas, unit types, price baskets, payment schedules, sales
policy), the honesty black list (inventory/scarcity/ROI fabrication bans),
and the persona/CTA-free contract. Also owns three regression contracts for
training mode: prompt-profile isolation in build_messages, the unknown
answer_mode 422, and the SSE routing-frame CTA filter + customer-redirect
guard never applying to the reserved scope.

All offline: LLM/DB seams are recording fakes, no credentials anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.application.services.generate import (
    _SYSTEM_PROMPT,
    _TRAINING_PROMPT,
    build_messages,
)
from api.application.services.merge import Merged
from api.application.services.project_scope import (
    TRAINING_PROJECT_KEY,
    is_training_scope,
)
from api.interfaces.api.main import QueryRequest, create_app
from tests._auth_seams import sales_bearer_headers

# --- 1. training policy: explicit lookup behaviors -----------------------------

_TRAINING_ANCHORS = (
    "PHẠM VI NỘI DUNG (tra cứu tối đa chi tiết)",
    "Giỏ hàng & bảng giá",
    "Lịch thanh toán",
    "Chính sách bán hàng",
    "diện tích thông thủy & tim tường",
    "CẤM BỊA DỮ LIỆU KINH DOANH",
    "TỒN KHO",
    "ROI / tỷ suất cho thuê",
    "SỰ KHAN HIẾM / deadline",
    "CHỈ tin vào dữ liệu trong RAG_CONTEXT và FACT_EVIDENCE",
    "LEO THANG (escalation tới con người)",
    "tối đa chi tiết",
)


def test_training_prompt_has_explicit_lookup_and_honesty_behaviors() -> None:
    # Whitespace-normalized haystack: prompt prose wraps mid-phrase in the md.
    upper = " ".join(_TRAINING_PROMPT.upper().split())
    for anchor in _TRAINING_ANCHORS:
        needle = " ".join(anchor.upper().split())
        assert needle in upper, f"missing lookup anchor: {anchor}"


def test_training_prompt_stays_persona_free_and_cta_free() -> None:
    # The lookup profile must never carry the customer persona/CTA machinery.
    forbidden = (
        "{ten_thuong_mai}",
        "{vi_tri}",
        "chuyên viên tư vấn cao cấp",
        "SALES_CONTEXT",
        "CONVERSATION_DIRECTIVE",
        "lead_cta_hint",
        "mời khách để lại số",
        "cuộc gọi 5 phút",
    )
    for marker in forbidden:
        assert marker not in _TRAINING_PROMPT


def test_training_urgency_requires_dated_evidence_only() -> None:
    urgent = _TRAINING_PROMPT.upper()
    assert "NGÀY" in urgent
    # Scarcity/urgency may only ride dated sources + the black list shares the file.
    assert "mốc hiệu lực CÓ NGÀY kèm nguồn" in _TRAINING_PROMPT
    assert "ưu đãi hết hôm nay" in _TRAINING_PROMPT


# --- 2. customer policy: consent/opt-out + scarcity fabrication ban ------------


def test_customer_prompt_has_consent_opt_out_and_scarcity_ban() -> None:
    assert "Consent/opt-out" in _SYSTEM_PROMPT
    assert "KHÔNG xin lại lần nào nữa trong session" in _SYSTEM_PROMPT
    assert "LIÊM THẬT VỀ TỒN KHO & URGENCY" in _SYSTEM_PROMPT
    assert "KHÔNG tạo cảm giác khan hiếm giả" in _SYSTEM_PROMPT
    # Regressions pinned by older tests still hold after the edit.
    assert "QUY TẮC CỨNG" in _SYSTEM_PROMPT
    assert len(_SYSTEM_PROMPT) > 3000


def test_customer_modes_are_distinct_profiles() -> None:
    assert _TRAINING_PROMPT != _SYSTEM_PROMPT
    assert "TRA CỨU THÔNG TIN DỰ ÁN" in _TRAINING_PROMPT.upper()


# --- 3. build_messages isolation with the expanded training profile ------------

_FORBIDDEN_IN_TRAINING_MESSAGES = (
    "SALES_CONTEXT",
    "CONVERSATION_DIRECTIVE",
    "mời khách để lại số",
    "{ten_thuong_mai}",
    "khuyên khách để lại số",
)


def _merged(project_key: str, **extra_meta) -> Merged:
    meta = {
        "rewritten": "Cách khai thác nhu cầu khách cho căn 2PN?",
        "query": "Cách khai thác nhu cầu khách cho căn 2PN?",
        "project_key": project_key,
    }
    meta.update(extra_meta)
    return Merged(rag_blocks="RAG", evidence_blocks="EV", sources=[], facts=[], meta=meta)


def test_training_build_messages_keep_profile_isolation_after_v2_expansion() -> None:
    merged = _merged(
        TRAINING_PROJECT_KEY,
        conversation_directive="Recap + mời nhận cuộc gọi.",
        lead_cta_hint="customer CTA",
    )
    messages = build_messages(merged, [])
    systems = [m for m in messages if m["role"] == "system"]
    assert len(systems) == 1  # directive message never joins the training profile
    assert systems[0]["content"] == _TRAINING_PROMPT
    blob = json.dumps(messages, ensure_ascii=False)
    for marker in _FORBIDDEN_IN_TRAINING_MESSAGES:
        assert marker not in blob
    assert merged.meta.get("sales_context_injected") is None


def test_customer_build_messages_unaffected_by_training_expansion() -> None:
    from api.application.services.project_config import render_template

    merged = _merged("camellia", conversation_directive="chào ấm 1 câu")
    messages = build_messages(merged, [])
    assert messages[0]["content"] == render_template(_SYSTEM_PROMPT, "camellia")
    assert messages[0]["content"] != _TRAINING_PROMPT


# --- 4. unknown answer_mode -> 422 ----------------------------------------------


def test_unknown_answer_mode_rejected_at_schema() -> None:
    with pytest.raises(ValidationError) as excinfo:
        QueryRequest.model_validate({"query": "coaching", "answer_mode": "coaching-pro"})
    assert any(
        err["loc"][-1] == "answer_mode" for err in excinfo.value.errors()
    )


class _RecordingGate:
    def __init__(self) -> None:
        self.refunds = 0

    async def prepare_turn(self, **kwargs):
        return _AttributedTurnContext()

    async def refund_turn(self, context):
        self.refunds += 1


class _AttributedTurnContext:
    identity_key = "dev:device-prompt-anchor-test"
    project_key = TRAINING_PROJECT_KEY

    def quota_payload(self) -> dict:
        return {"used_turns": None}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: _RecordingGate(),
    )

    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key", _fake_resolve
    )
    return TestClient(create_app())


def test_query_endpoint_returns_422_for_unknown_answer_mode(client) -> None:
    resp = client.post(
        "/query", json={"query": "coaching", "project_key": "camellia", "answer_mode": "demo"}
    )
    assert resp.status_code == 422
    details = resp.json()["detail"]
    assert any(err.get("loc", [None])[-1] == "answer_mode" for err in details)


# --- 5. transport isolation: routing frame carries no CTA in training -----------


_PAYLOAD = {
    "answer": "lookup answer",
    "sources": [{"doc_id": "price-a", "kind": "price", "title": "T"}],
    "facts": [],
    "images": [],
    "videos": [],
    "places": [],
    "confidence": "HIGH",
    "requires_review": False,
    "routing": {"intent": "rag"},
    "trace_id": "t-9",
    "latency_ms": 1,
}


def test_sse_routing_frame_strips_lead_cta_in_training(
    client, monkeypatch, local_rsa_jwk, offline_auth_seams
) -> None:
    async def ok_run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        assert is_training_scope(kwargs["project_key"])
        if on_event is not None:
            await on_event("routing", {"intent": "rag", "lead_cta_hint": "customer CTA"})
        return {**_PAYLOAD, "lead_cta_hint": "customer CTA"}

    class FakePipeline:
        run = ok_run

    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", FakePipeline
    )
    with client.stream(
        "POST",
        "/query",
        json={
            "query": "coaching",
            "answer_mode": "training",
            "session_id": "s-training-prompt-anchor",
            "context": {"project_key": "camellia"},
        },
        headers={
            "accept": "text/event-stream",
            **sales_bearer_headers(local_rsa_jwk),
        },
    ) as stream:
        raw = "".join(stream.iter_text())
    assert "customer CTA" not in raw  # stripped from BOTH routing and done frames
    assert '"kind": "price"' in raw  # shared project-corpus sources ride through untouched
    assert "event: done" in raw


# --- 6. source pin: customer redirect guard skips the training scope ------------


def test_conv_redirect_guard_ordered_before_training_branch() -> None:
    """FR-25: cross-project redirect detection is disabled for _training BEFORE
    any redirect template could short-circuit the inner retrieval workflow."""
    src = (
        Path(__file__).resolve().parents[1]
        / "api"
        / "application"
        / "pipelines"
        / "conv_workflow.py"
    ).read_text(encoding="utf-8")
    guard_at = src.find("if is_training_scope(project_key)")
    detect_at = src.find("detect_foreign_project(query")
    assert guard_at != -1, "training-scope guard vanished from start_conv"
    assert detect_at != -1
    assert guard_at < detect_at  # guard decides before the customer detector runs
