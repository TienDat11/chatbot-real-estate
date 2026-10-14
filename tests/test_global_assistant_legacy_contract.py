"""Regression sealing tests for GA-01 — freeze legacy chatbot demo contracts.

Seals HTTP-observable legacy behaviours from the GA-01 ticket. Where a behaviour
is already strongly pinned by a sibling regression file, this module points to it:

  - Foreign-project redirect: tests/test_project_redirect_guard.py
    ::test_soleil_question_in_camellia_redirects_without_retrieval
  - _post_filter isolation (drops foreign-project chunks at retrieval):
    tests/test_project_isolation_regression.py
    ::test_post_filter_soleil_query_keeps_only_soleil_policy_chunks
  - Training auth / 409 preflight codes: tests/test_query_training_auth.py
    and tests/test_query_training_conflict_codes.py
  - Query history normalisation (SSE/JSON parity, truncation):
    tests/test_query_api.py::test_json_and_sse_success_preserve_semantic_payload

No production code is modified by or for these tests.
"""

from __future__ import annotations

import time
import uuid as uuid_mod
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.application.services.query_quota_gate import UnmanagedTurnContext
from api.interfaces.api.main import create_app

_PAYLOAD: dict[str, Any] = {
    "answer": "ok", "sources": [{"doc_id": "d-1", "kind": "price", "title": "T"}],
    "facts": [], "images": [{"image_id": "img-1", "url_cdn": "https://cdn.example/img-1"}],
    "videos": [], "places": [], "confidence": "HIGH", "requires_review": False,
    "routing": {"intent": "rag"}, "trace_id": "t-1", "latency_ms": 1,
}

# Fake catalogue for the 422 PROJECT_SCOPE body (ordering + identity shape).
_CATALOGUE_2_ACTIVE = [
    {"project_key": "camellia", "display_name": "The Camellia Son Tra - Da Nang",
     "short_name": "The Camellia Son Tra", "is_hot": True},
    {"project_key": "soleil", "display_name": "The Soleil Đà Nẵng",
     "short_name": "The Soleil", "is_hot": False},
]


class _FakePipeline:
    last_project_key: str | None = None
    calls: list[dict[str, Any]] = []

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        pk = kwargs["project_context"].project_key
        _FakePipeline.last_project_key = pk
        _FakePipeline.calls.append({"query": query, "project_key": pk})
        return dict(_PAYLOAD)


class _OfflineQuotaGate:
    async def prepare_turn(self, **kwargs):
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        return None


class _RecordingQuotaGate(_OfflineQuotaGate):
    """Records that the quota gate was (or was not) consulted.

    D4's BLOCK-no-scope control is the real ``/query`` boundary, so the proof
    must be that a refused turn reserved nothing: a quota spy is the only way
    to observe the absence of work, the pipeline spy cannot see it either.
    """

    prepared: list[dict[str, Any]] = []
    refunded: list[Any] = []

    async def prepare_turn(self, **kwargs):
        _RecordingQuotaGate.prepared.append(kwargs)
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        _RecordingQuotaGate.refunded.append(context)
        return None



async def _fake_resolve(project_key, *, active_projects=None):
    """Explicit-key passthrough only — no implicit default. Production raises
    ProjectScopeError when no key is given and >1 active project exists; that
    422 path is sealed by _client_with_multiple_active below."""
    if project_key is None:
        from api.application.services.project_scope import ProjectScopeError

        raise ProjectScopeError("project_key is required")
    return project_key


def _registry_records():
    """Two active registry records — the DB snapshot /query resolves against."""
    from api.application.ports.project_registry import ProjectRegistryRecord

    return [
        ProjectRegistryRecord(
            project_key=row["project_key"],
            ten_thuong_mai=row["display_name"],
            ten_phap_ly=None,
            vi_tri=None,
            hotline=None,
            location=None,
            geo_center_lat=None,
            geo_center_lng=None,
            is_hot=row["is_hot"],
            status="active",
        )
        for row in _CATALOGUE_2_ACTIVE
    ]


def _install_offline_seams(monkeypatch, *, quota_gate: Any = None):
    _FakePipeline.last_project_key = None
    _FakePipeline.calls = []
    _RecordingQuotaGate.prepared = []
    _RecordingQuotaGate.refunded = []
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: (quota_gate or _OfflineQuotaGate)(),
    )
    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key", _fake_resolve
    )
    # /query reads the registry snapshot before scope resolution and refuses an
    # empty catalogue (ProjectContext raises). Without this stub the suite only
    # passes where a live project registry is reachable.
    async def _fetch_records():
        return _registry_records()

    monkeypatch.setattr(
        "api.application.services.project_scope.fetch_active_project_records",
        _fetch_records,
    )
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", _FakePipeline
    )


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    _install_offline_seams(monkeypatch)
    return TestClient(create_app())


def _client_with_multiple_active(
    monkeypatch, *, honour_explicit_key: bool = False, quota_gate: Any = None
) -> TestClient:
    """App where omitting project_key yields 422 PROJECT_SCOPE (>1 active).

    ``honour_explicit_key`` is the opposite control: the resolver still refuses
    an OMITTED key under the same two-project catalogue, but passes an explicit
    one through, proving the 422 is scope AMBIGUITY and not a blanket refusal.
    """
    from api.application.services.project_scope import (
        PROJECT_CHOICE_REQUIRED,
        ProjectScopeError,
    )

    from api.application.ports.project_registry import ProjectRegistryRecord

    async def fetch_active_projects():
        return [
            ProjectRegistryRecord(
                project_key=key, ten_thuong_mai=name, ten_phap_ly=None, vi_tri=None,
                hotline=None, location=None, geo_center_lat=None, geo_center_lng=None,
                is_hot=key == "camellia", status="active",
            )
            for key, name in (("camellia", "The Camellia Son Tra - Da Nang"), ("soleil", "The Soleil Đà Nẵng"))
        ]

    async def resolve_choice(requested, *, active_projects=None):
        if honour_explicit_key and requested:
            return requested
        raise ProjectScopeError(PROJECT_CHOICE_REQUIRED)

    _install_offline_seams(monkeypatch, quota_gate=quota_gate)
    monkeypatch.setattr("api.application.services.project_scope.fetch_active_projects", fetch_active_projects)
    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", resolve_choice)
    return TestClient(create_app())


def test_project_scope_error_formats_picker_from_single_active_snapshot(monkeypatch) -> None:
    from api.application.ports.project_registry import ProjectRegistryRecord
    from api.application.services.project_scope import ProjectScopeError

    records = [
        ProjectRegistryRecord(
            project_key="camellia", ten_thuong_mai="The Camellia Son Tra - Da Nang",
            ten_phap_ly=None, vi_tri=None, hotline=None, location="Da Nang",
            geo_center_lat=16.1, geo_center_lng=108.2, is_hot=True, status="active",
        ),
        ProjectRegistryRecord(
            project_key="soleil", ten_thuong_mai="The Soleil Đà Nẵng",
            ten_phap_ly=None, vi_tri=None, hotline=None, location="Da Nang",
            geo_center_lat=16.2, geo_center_lng=108.3, is_hot=False, status="active",
        ),
    ]
    reads = []

    _install_offline_seams(monkeypatch)

    async def reject(requested, *, active_projects=None):
        raise ProjectScopeError("bad project")

    async def fetch_records():
        reads.append(None)
        return records if len(reads) == 1 else []
    monkeypatch.setattr("api.application.services.project_scope.fetch_active_project_records", fetch_records)
    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", reject)
    response = TestClient(create_app()).post("/query", json={"query": "hello", "project_key": "missing"})

    assert response.status_code == 422
    assert len(reads) == 1
    projects = response.json()["projects"]
    assert [item["project_key"] for item in projects] == ["camellia", "soleil"]
    assert set(projects[0]) == {"project_key", "name", "display_name", "short_name", "location", "lat", "lng", "is_hot"}
    assert projects[0]["lat"] == 16.1 and projects[0]["is_hot"] is True



# --- 1. POST /query accepts the current request contract (explicit key). ---


def test_legacy_query_with_explicit_project_still_works(client) -> None:
    """Explicit project_key → HTTP 200, status=completed, key threaded to pipeline."""
    resp = client.post("/query", json={"query": "Bảng giá căn mẫu?", "project_key": "camellia"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["answer"]
    assert isinstance(body["sources"], list)
    assert body["project_redirect"] is None
    assert _FakePipeline.last_project_key == "camellia"
    assert len(_FakePipeline.calls) == 1


# --- 2. Multiple active + no explicit key → 422 PROJECT_SCOPE + catalogue. ---


def test_legacy_query_without_project_with_multiple_active_projects_keeps_project_scope_error(
    monkeypatch,
) -> None:
    """Two active projects + no project_key → 422 PROJECT_SCOPE with the exact
    legacy Vietnamese choice message and an ordered catalogue for the FE picker."""
    client = _client_with_multiple_active(monkeypatch, quota_gate=_RecordingQuotaGate)
    resp = client.post("/query", json={"query": "Căn nào đẹp không?"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "PROJECT_SCOPE"
    assert body["error"]["message"] == "Vui lòng chọn dự án (có nhiều dự án đang mở bán)"
    projects = body["projects"]
    assert [p["project_key"] for p in projects] == ["camellia", "soleil"]
    for row in projects:
        assert set(row) >= {"project_key", "display_name", "short_name", "is_hot"}
        assert isinstance(row["project_key"], str) and row["project_key"]
        assert isinstance(row["display_name"], str) and row["display_name"]
        assert isinstance(row["short_name"], str)
        assert isinstance(row["is_hot"], bool)

    # D4: the refused turn must cost nothing. The scope gate runs BEFORE the
    # quota reservation, so no quota was prepared...
    assert _RecordingQuotaGate.prepared == []
    assert _RecordingQuotaGate.refunded == []
    # ...and no pipeline leg was ever constructed or run.
    assert _FakePipeline.calls == []
    assert _FakePipeline.last_project_key is None


def test_ambiguous_scope_refusal_is_not_a_blanket_refusal(monkeypatch) -> None:
    """Same two-project catalogue, explicit key → the turn proceeds.

    Without this the 422 above could pass for a blanket refusal of a
    multi-project deployment rather than a request for the user to choose.
    """
    client = _client_with_multiple_active(
        monkeypatch, honour_explicit_key=True, quota_gate=_RecordingQuotaGate
    )
    resp = client.post(
        "/query", json={"query": "Căn nào đẹp không?", "project_key": "camellia"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    assert _FakePipeline.last_project_key == "camellia"
    assert len(_FakePipeline.calls) == 1
    # The positive half: the gate is passed THROUGH, not skipped — quota ran.
    assert len(_RecordingQuotaGate.prepared) == 1


# --- 3. Session read remains project-scoped: wrong project → 404 (no leak). ---


def test_legacy_session_read_remains_project_scoped(monkeypatch) -> None:
    """GET /api/sessions/{id}/messages with a wrong project_key → 404; the
    session's transcript never leaks across project boundaries.

    (test_sessions_api.py pins device-identity mismatch → 404; this pins the
    separate project_key dimension that the service enforces in SQL.)
    """
    from api.application.services.anon_identity import mint_anonymous_identity_token
    from api.application.services.chat_history_service import ChatMessage, ChatSessionSummary
    from api.infrastructure.config.config import get_settings
    from api.interfaces.api import sessions as sessions_module

    session_summary = ChatSessionSummary(
        session_id="legacy-session-1", device_id="device-1",
        project_key="camellia", title="Tư vấn Camellia",
        message_count=2, handed_off=False,
        last_active_at="2025-01-01T00:00:00Z", identity_key="identity-1",
    )

    class _FakeHistoryService:
        async def session(self, *, session_id, device_id=None, project_key=None,
                          identity_key=None, staff_scoped=False):
            if session_id == "legacy-session-1" and project_key == "camellia":
                return session_summary
            return None

        async def messages(self, *, session_id):
            return [ChatMessage(
                role="user", content="Giá bao nhiêu?", meta={},
                created_at="2025-01-01T00:00:00Z",
            )]

    monkeypatch.setattr(sessions_module, "_service", lambda: _FakeHistoryService())
    client = TestClient(create_app())

    anon_token = mint_anonymous_identity_token(
        get_settings().anon_identity_secret,
        now_epoch_seconds=int(time.time()),
        subject=str(uuid_mod.UUID("00000000-0000-4000-8000-000000000001")),
    )

    resp = client.get(
        "/api/sessions/legacy-session-1/messages?project_key=soleil",
        headers={"X-Device-Id": "device-1", "X-Anon-Token": anon_token},
    )
    assert resp.status_code == 404, resp.text
    assert "messages" not in resp.json()

    resp_ok = client.get(
        "/api/sessions/legacy-session-1/messages?project_key=camellia",
        headers={"X-Device-Id": "device-1", "X-Anon-Token": anon_token},
    )
    assert resp_ok.status_code == 200, resp_ok.text
    body = resp_ok.json()
    assert body["session_id"] == "legacy-session-1"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "Giá bao nhiêu?"


# --- 5. Foreign-project redirect — fully pinned by
#   tests/test_project_redirect_guard.py. ---


# --- 6. Project isolation: pipeline receives exactly the requested project. ---


def test_legacy_project_isolation_still_blocks_cross_project_data(client) -> None:
    """A camellia-scoped query reaches the pipeline with exactly camellia.
    The pipeline never receives a foreign-project key — the upstream gate that
    makes _post_filter (see §5 pointer) effective at the HTTP boundary."""
    resp = client.post(
        "/query", json={"query": "Giá bất động sản?", "project_key": "camellia"}
    )
    assert resp.status_code == 200, resp.text
    assert _FakePipeline.last_project_key == "camellia"
    assert _FakePipeline.calls == [{"query": "Giá bất động sản?", "project_key": "camellia"}]
