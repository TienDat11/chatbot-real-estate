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

from api.domain.value_objects.constants import MAX_QUERY_LENGTH
from api.interfaces.api.main import (
    MAX_HISTORY_CONTENT_LENGTH,
    HistoryTurn,
    QueryRequest,
    _normalize_history,
    create_app,
)

# Minimal payload satisfying QueryResponse so the fake pipeline can answer.
_PAYLOAD = {
    "answer": "ok",
    "sources": [],
    "facts": [],
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

    async def run(self, query, session_id, as_of, history, on_event=None):
        FakePipeline.last_history = history
        return dict(_PAYLOAD)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    # The handler imports RagQueryPipelineConv lazily, so patching the module
    # attribute swaps the pipeline for both JSON and SSE paths.
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", FakePipeline
    )
    return TestClient(create_app())


def test_query_request_accepts_long_history_content():
    """History longer than the query cap must validate (no 422 at schema level)."""
    long_answer = "A" * (MAX_QUERY_LENGTH + 2000)
    req = QueryRequest.model_validate(
        {"query": "Can P-01 gia bao nhieu?", "history": [{"role": "assistant", "content": long_answer}]}
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
