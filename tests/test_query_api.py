"""Regression tests for /query history handling.

FE replays the last 8 messages with full content, and assistant RAG answers
can exceed 2000 chars. The schema must accept long history turns (cap is
DoS-only), and the server must truncate them to MAX_QUERY_LENGTH and drop
blank turns before the pipeline runs — on both the JSON and SSE paths.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.application.services.query_quota_gate import UnmanagedTurnContext
from api.domain.value_objects.constants import MAX_QUERY_LENGTH
from api.interfaces.api.main import (
    MAX_HISTORY_CONTENT_LENGTH,
    HistoryTurn,
    QueryRequest,
    _normalize_history,
    create_app,
)
from tests._auth_seams import sales_bearer_headers
from tests._training_seams import install_training_seams, training_payload


class _OfflineQuotaGate:
    """Test-only quota seam; production keeps the fail-closed gate unchanged."""

    async def prepare_turn(self, **kwargs):
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        return None


# Minimal payload satisfying QueryResponse so the fake pipeline can answer.
_PAYLOAD = {
    "answer": "ok",
    "sources": [],
    "facts": [],
    "images": [{"image_id": "img-1", "url_cdn": "https://cdn.example/img-1"}],
    "videos": [{"title": "Tour", "url_cdn": "https://cdn.example/tour.mp4"}],
    "places": [],
    "confidence": "HIGH",
    "requires_review": False,
    "routing": {"intent": "rag"},
    "trace_id": "t-1",
    "latency_ms": 1,
}


class FakePipeline:
    """Captures the normalized history the /query handler passes down."""

    last_history: list[dict[str, str]] | None = None

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        FakePipeline.last_history = history
        return dict(_PAYLOAD)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    # These history normalization tests are intentionally offline. Override
    # only the composition root; production remains fail-closed on store errors.
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: _OfflineQuotaGate(),
    )
    # The handler imports RagQueryPipelineConv lazily, so patching the module
    # attribute swaps the pipeline for both JSON and SSE paths.
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", FakePipeline
    )

    # The handler also resolves the project scope lazily; a fake keeps the tests
    # DB-free while exercising the post-resolution handler flow.
    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", _fake_resolve)
    return TestClient(create_app())


def test_query_request_answer_mode_is_strict():
    assert QueryRequest(query="coaching", answer_mode="normal").answer_mode == "normal"
    assert (
        QueryRequest(
            query="coaching", answer_mode="training", context={"project_key": "camellia"}
        ).answer_mode
        == "training"
    )
    assert QueryRequest(query="coaching").answer_mode is None
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "coaching", "answer_mode": "unknown"})
    # G3-r6 §10.2 cross-field rules: context is exactly training, and only
    # training (both directions 422); extra context keys are rejected.
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "coaching", "answer_mode": "training"})
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"query": "coaching", "context": {"project_key": "camellia"}})
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(
            {
                "query": "coaching",
                "answer_mode": "training",
                "context": {"project_key": "camellia", "admin": True},
            }
        )


def test_query_training_uses_reserved_scope_and_suppresses_cta(
    client, monkeypatch, offline_auth_seams, local_rsa_jwk
):
    install_training_seams(monkeypatch)
    captured = {}

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        captured.update(kwargs)
        return {
            **_PAYLOAD,
            "lead_cta_hint": "customer CTA",
            "project_redirect": {"project_key": "camellia"},
        }

    monkeypatch.setattr(FakePipeline, "run", run)
    response = client.post(
        "/query",
        json=training_payload(project_key="camellia"),
        headers=sales_bearer_headers(local_rsa_jwk),
    )
    assert response.status_code == 200
    assert captured["project_key"] == "_training"
    # G3-r6: the named context project rides to the pipeline as its own
    # optional layer; normal-mode callers never see it.
    assert captured["training_context_project_key"] == "camellia"
    body = response.json()
    assert body["lead_cta_hint"] is None
    assert body["project_redirect"] is None
    # The terminal payload mirrors the private session binding.
    assert body["training_session_id"] == "s-training-1"
    assert body["context"] == {"project_key": "camellia"}


def test_query_request_accepts_long_history_content():
    """History longer than the query cap must validate (no 422 at schema level)."""
    long_answer = "A" * (MAX_QUERY_LENGTH + 2000)
    req = QueryRequest.model_validate(
        {
            "query": "Can P-01 gia bao nhieu?",
            "history": [{"role": "assistant", "content": long_answer}],
        }
    )
    assert req.history is not None
    assert len(req.history[0].content) == len(long_answer)


def test_history_turn_dos_cap_still_enforced():
    """The relaxed cap is DoS-only: content beyond it still rejects."""
    with pytest.raises(ValidationError):
        HistoryTurn(role="assistant", content="x" * (MAX_HISTORY_CONTENT_LENGTH + 1))


def test_history_turn_rejects_empty_content():
    with pytest.raises(ValidationError):
        HistoryTurn(role="user", content="")


def test_history_turn_rejects_invalid_role():
    with pytest.raises(ValidationError):
        HistoryTurn(role="system", content="hello")


def test_normalize_history_truncates_and_drops_blank():
    turns = [
        HistoryTurn(role="user", content="a" * (MAX_QUERY_LENGTH + 1000)),
        HistoryTurn(role="assistant", content="   "),
        HistoryTurn(role="assistant", content="b" * (MAX_QUERY_LENGTH + 500)),
    ]
    out = _normalize_history(turns)
    assert [t["role"] for t in out] == ["user", "assistant"]
    assert all(len(t["content"]) == MAX_QUERY_LENGTH for t in out)


def test_normalize_history_none_is_empty():
    assert _normalize_history(None) == []


def test_query_json_long_history_no_422_and_truncated(client):
    FakePipeline.last_history = None
    resp = client.post(
        "/query",
        json={
            "query": "Can P-01 gia bao nhieu?",
            "history": [
                {"role": "user", "content": "hoi ve du an"},
                {"role": "assistant", "content": "A" * 4000},
                {"role": "user", "content": "   "},
            ],
        },
    )
    assert resp.status_code == 200
    hist = FakePipeline.last_history
    assert hist is not None
    # Blank turn dropped; long assistant answer truncated to the pipeline cap.
    assert [t["role"] for t in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "hoi ve du an"
    assert len(hist[1]["content"]) == MAX_QUERY_LENGTH


def test_query_sse_long_history_no_422_and_truncated(client):
    FakePipeline.last_history = None
    resp = client.post(
        "/query",
        headers={"Accept": "text/event-stream"},
        json={
            "query": "tiep tuc",
            "history": [{"role": "assistant", "content": "B" * 4000}],
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    hist = FakePipeline.last_history
    assert hist is not None
    assert len(hist) == 1
    assert len(hist[0]["content"]) == MAX_QUERY_LENGTH


def _sse_frames(body: str) -> list[tuple[str, dict]]:
    frames = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        frames.append((lines["event"], __import__("json").loads(lines.get("data", "{}"))))
    return frames


def test_json_and_sse_success_preserve_semantic_payload(client):
    """JSON and terminal SSE done carry identical answer/media/facts/sources."""
    json_response = client.post("/query", json={"query": "gia can", "project_key": "camellia"})
    assert json_response.status_code == 200
    json_payload = json_response.json()

    sse_response = client.post(
        "/query",
        headers={"Accept": "text/event-stream"},
        json={"query": "gia can", "project_key": "camellia"},
    )
    assert sse_response.status_code == 200
    frames = _sse_frames(sse_response.text)
    assert frames[-1][0] == "done"
    done = frames[-1][1]
    for key in ("answer", "images", "videos", "facts", "sources", "status", "history_persisted"):
        assert done[key] == json_payload[key]
