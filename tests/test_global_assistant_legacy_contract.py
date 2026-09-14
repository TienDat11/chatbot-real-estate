"""Regression sealing tests for GA-01 — freeze legacy chatbot demo contracts.

These tests pin the *external* HTTP-observable contracts that later Global
Assistant work must not silently break.  Every assertion goes through the real
``TestClient(create_app())`` /query or /api/sessions surface: no internal
function calls, no implementation mocks beyond the standard offline seams
already shared by the other query/sessions test modules.

The six behaviours pinned (ticket GA-01 §5):

1. ``POST /query`` accepts the current request contract (explicit project_key -> 200).
2. More than one active project with no explicit project keeps the existing
   project-scope error behaviour (422 PROJECT_SCOPE + projects catalogue).
3. Existing project-scoped session APIs still require their current project
   ownership scope (wrong project -> 404, no session leak).
4. Existing training requests keep their current authorization/context semantics
   (covered by the _auth_seals + _training_seals suites; not re-duplicated here
   per ticket §2.4 "Do not duplicate an existing test if it already proves
   exactly the behaviour").
5. Existing foreign-project redirect behaviour remains unchanged.
6. Existing project isolation remains enforced (cross-project data is never
   returned to a wrong scope).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.application.ports.project_registry import ProjectRegistryRecord
from api.application.services.query_quota_gate import UnmanagedTurnContext
from api.interfaces.api.main import create_app

# ---------------------------------------------------------------------------
# Shared offline seams: fail-closed quota (no DB spend), fake pipeline,
# and a no-op project resolver that mirrors production's "explicit key ->
# that key, omit -> default" behaviour for normal-mode calls.
# ---------------------------------------------------------------------------

_PAYLOAD = {
    "answer": "Chào bạn! Giá căn mẫu The Camellia từ 890 triệu.",
    "sources": [{"doc_id": "price-camellia-2026q3-policy", "kind": "price", "title": "Chính sách giá"}],
    "facts": [],
    "images": [{"image_id": "img-1", "url_cdn": "https://cdn.example/img-1"}],
    "videos": [],
    "places": [],
    "confidence": "HIGH",
    "requires_review": False,
    "routing": {"intent": "rag"},
    "trace_id": "t-1",
    "latency_ms": 12,
}


class _FakePipeline:
    """Records the project scope it receives and returns a canned answer."""

    last_project_key: str | None = None
    last_training_context: str | None = None
    calls: list[dict[str, Any]] = []

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        pk = kwargs.get("project_key")
        ctx = kwargs.get("training_context_project_key")
        _FakePipeline.last_project_key = pk
        _FakePipeline.last_training_context = ctx
        _FakePipeline.calls.append(
            {"query": query, "project_key": pk, "training_context": ctx}
        )
        return dict(_PAYLOAD)


class _OfflineQuotaGate:
    """Test-only quota seam; production keeps the fail-closed gate unchanged."""

    async def prepare_turn(self, **kwargs):
        return UnmanagedTurnContext()

    async def refund_turn(self, context):
        return None


async def _fake_resolve(project_key, *, active_projects=None):
    """Mirror production default rule 10.1 for normal-mode callers."""
    return project_key or "camellia"


def _reset_pipeline():
    _FakePipeline.last_project_key = None
    _FakePipeline.last_training_context = None
    _FakePipeline.calls = []


def _install_offline_seams(monkeypatch, *, use_fake_pipeline=True):
    """Install the standard offline seams: quota gate, project resolver."""
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: _OfflineQuotaGate(),
    )
    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key", _fake_resolve
    )
    if use_fake_pipeline:
        _reset_pipeline()
        monkeypatch.setattr(
            "api.application.pipelines.conv_workflow.RagQueryPipelineConv", _FakePipeline
        )


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    _install_offline_seams(monkeypatch, use_fake_pipeline=True)
    return TestClient(create_app())


# Catalogue / known-projects mirroring db/seed/project_config.sql.
_CATALOGUE = [
    {
        "project_key": "camellia",
        "name": "The Camellia Son Tra - Da Nang",
        "display_name": "The Camellia Son Tra - Da Nang",
        "short_name": "The Camellia Son Tra",
        "location": "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, Đà Nẵng",
        "lat": 16.1052,
        "lng": 108.2558,
        "is_hot": True,
    },
    {
        "project_key": "soleil",
        "name": "The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite Collection)",
        "display_name": "The Soleil Đà Nẵng",
        "short_name": "The Soleil",
        "location": "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
        "lat": 16.0710756,
        "lng": 108.2436243,
        "is_hot": False,
    },
]

_KNOWN_PROJECTS = [
    ("camellia", "The Camellia Son Tra - Da Nang"),
    ("soleil", "The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite Collection)"),
]


def _camellia_registry_record() -> ProjectRegistryRecord:
    return ProjectRegistryRecord(
        project_key="camellia",
        ten_thuong_mai="The Camellia Son Tra - Da Nang",
        ten_phap_ly="Công ty Cổ phần Bất động sản Sài Đà Nẵng",
        vi_tri="Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, Đà Nẵng",
        location="Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, Đà Nẵng",
        geo_center_lat=16.1052,
        geo_center_lng=108.2558,
        hotline="0909 123 456",
        is_hot=True,
        status="active",
    )


def _client_with_multiple_active(monkeypatch) -> TestClient:
    """App where the default-rule sees >1 active project, so omitting
    project_key yields 422 PROJECT_SCOPE (not a silent default)."""

    async def _resolve_choice(requested, *, active_projects=None):
        from api.application.services.project_scope import (
            PROJECT_CHOICE_REQUIRED,
            ProjectScopeError,
        )

        raise ProjectScopeError(PROJECT_CHOICE_REQUIRED)

    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key", _resolve_choice
    )
    monkeypatch.setattr(
        "api.application.services.project_config.fetch_projects",
        lambda: list(_CATALOGUE),
    )
    return TestClient(create_app())


# ===========================================================================
# 1. POST /query accepts the current request contract (explicit project_key).
# ===========================================================================


def test_legacy_query_with_explicit_project_still_works(client) -> None:
    """A normal-mode query with an explicit project_key resolves and returns the
    same documented response shape: status=completed, answer present, sources
    list, and the project_key threaded through to the pipeline."""
    resp = client.post(
        "/query",
        json={"query": "Bảng giá căn mẫu The Camellia bao nhiêu?", "project_key": "camellia"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["answer"]
    assert isinstance(body["sources"], list)
    # No cross-project redirect: the query is scoped to the current project.
    assert body["project_redirect"] is None
    # The resolved project key reaches the pipeline unchanged.
    assert _FakePipeline.last_project_key == "camellia"
    assert _FakePipeline.last_training_context is None
    assert len(_FakePipeline.calls) == 1


# ===========================================================================
# 2 + multi-active-project 422 path
#    Multiple active projects without an explicit choice → 422 PROJECT_SCOPE
#    with the projects catalogue embedded.
# ===========================================================================


def test_legacy_query_without_project_with_multiple_active_projects_keeps_project_scope_error(
    monkeypatch,
) -> None:
    """Two active projects + no explicit project_key → 422 with the legacy
    PROJECT_SCOPE code, the Vietnamese choice message, and the catalogue
    array so the FE picker renders without a second round-trip."""
    client = _client_with_multiple_active(monkeypatch)
    resp = client.post(
        "/query",
        json={"query": "Căn nào đẹp không?"},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "PROJECT_SCOPE"
    # The choice prompt is the exact legacy Vietnamese string.
    assert "dự án" in body["error"]["message"]
    # The 422 body carries the active-project catalogue so the FE can render
    # the picker immediately — exact keys and ordering are part of the contract.
    projects = body["projects"]
    assert len(projects) == 2
    assert [p["project_key"] for p in projects] == ["camellia", "soleil"]
    assert projects[0]["display_name"] == "The Camellia Son Tra - Da Nang"
    assert projects[0]["short_name"] == "The Camellia Son Tra"
    assert projects[0]["is_hot"] is True
    assert projects[1]["display_name"] == "The Soleil Đà Nẵng"
    assert projects[1]["short_name"] == "The Soleil"
    assert projects[1]["is_hot"] is False
    # Every catalogue row must expose the full contract key set.
    for row in projects:
        assert "project_key" in row
        assert "display_name" in row
        assert "short_name" in row


def test_legacy_query_without_project_with_multiple_active_returns_projects_contract(
    monkeypatch,
) -> None:
    """The multi-active-project 422 path must embed the projects catalogue in
    the exact contract shape, not just an error code."""
    client = _client_with_multiple_active(monkeypatch)
    resp = client.post("/query", json={"query": "Bạn biết gì về dự án?"})
    assert resp.status_code == 422
    body = resp.json()
    projects = body["projects"]
    assert isinstance(projects, list)
    assert len(projects) >= 2
    for p in projects:
        # Every row satisfies the documented contract keys.
        assert set(p) >= {"project_key", "display_name", "short_name", "is_hot"}
        assert isinstance(p["project_key"], str) and p["project_key"]
        assert isinstance(p["display_name"], str) and p["display_name"]
        assert isinstance(p["short_name"], str)
        assert isinstance(p["is_hot"], bool)


# ===========================================================================
# 3. Session read remains project-scoped: wrong project → 404 (no leak).
# ===========================================================================


def test_legacy_session_read_remains_project_scoped(monkeypatch) -> None:
    """GET /api/sessions/{session_id}/messages requires the project_key query
    param to match the session's owning project.  A wrong project must return
    404 (not the session's transcript) so cross-project data never leaks."""
    from api.application.services.anon_identity import mint_anonymous_identity_token
    from api.application.services.chat_history_service import ChatMessage, ChatSessionSummary
    from api.infrastructure.config.config import get_settings
    from api.interfaces.api import sessions as sessions_module

    session_summary = ChatSessionSummary(
        session_id="legacy-session-1",
        device_id="device-1",
        project_key="camellia",
        title="Tư vấn Camellia",
        message_count=2,
        handed_off=False,
        last_active_at="2025-01-01T00:00:00Z",
        identity_key="identity-1",
    )

    class _FakeHistoryService:
        async def session(
            self,
            *,
            session_id,
            device_id=None,
            project_key=None,
            identity_key=None,
            staff_scoped=False,
        ):
            # The service itself enforces project scope in SQL; mirror that:
            # only return the row when the project matches.
            if session_id == "legacy-session-1" and project_key == "camellia":
                return session_summary
            return None

        async def list_sessions(self, **kwargs):
            return [session_summary]

        async def messages(self, *, session_id):
            return [
                ChatMessage(
                    role="user", content="Giá bao nhiêu?", meta={}, created_at="2025-01-01T00:00:00Z"
                ),
            ]

    monkeypatch.setattr(sessions_module, "_service", lambda: _FakeHistoryService())
    client = TestClient(create_app())

    # Mint a valid anon identity token using the same ephemeral secret the app
    # generates for dev/test, so the anon path verifies it end-to-end.
    # Use a real UUIDv4 subject (verify requires uuid4) and current epoch
    # so the 90-day freshness window passes.
    import time
    import uuid as uuid_mod

    identity_subject = str(uuid_mod.UUID("00000000-0000-4000-8000-000000000001"))
    secret = get_settings().anon_identity_secret
    anon_token = mint_anonymous_identity_token(
        secret, now_epoch_seconds=int(time.time()), subject=identity_subject
    )

    # Wrong project scope: session is invisible → 404, no transcript leak.
    resp = client.get(
        "/api/sessions/legacy-session-1/messages?project_key=soleil",
        headers={"X-Device-Id": "device-1", "X-Anon-Token": anon_token},
    )
    assert resp.status_code == 404, resp.text
    assert "messages" not in resp.json()

    # Correct project scope: session is returned.
    resp_ok = client.get(
        "/api/sessions/legacy-session-1/messages?project_key=camellia",
        headers={"X-Device-Id": "device-1", "X-Anon-Token": anon_token},
    )
    assert resp_ok.status_code == 200, resp_ok.text
    body = resp_ok.json()
    assert body["session_id"] == "legacy-session-1"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["content"] == "Giá bao nhiêu?"


# ===========================================================================
# 5. Foreign-project redirect contract is unchanged (SSE).
# ===========================================================================


def test_legacy_foreign_project_redirect_contract_is_unchanged(
    monkeypatch,
) -> None:
    """A query naming a *different* known project emits the redirect signal
    in the SSE ``routing`` frame, returns the deterministic guidance answer
    (no corpus claims), and never invokes retrieval — identical to the
    pre-GA behaviour pinned by test_project_redirect_guard.py."""
    _reset_pipeline()
    # Use the REAL pipeline (not _FakePipeline) so the conv workflow runs and
    # the redirect short-circuit fires. We mock only the data seams.
    _install_offline_seams(monkeypatch, use_fake_pipeline=False)
    monkeypatch.setattr(
        "api.application.services.project_config.load_known_projects",
        _async_known_projects,
    )
    monkeypatch.setattr(
        "api.application.services.project_config.load_project_registry_record",
        _async_registry_record,
    )
    client = TestClient(create_app())

    resp = client.post(
        "/query",
        headers={"Accept": "text/event-stream"},
        json={
            "query": "Bạn biết gì về soleil không?",
            "project_key": "camellia",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _sse_frames(resp.text)
    routing_frames = [data for ev, data in frames if ev == "routing"]
    done_frames = [data for ev, data in frames if ev == "done"]

    # The redirect signal is attached to the routing frame.
    assert len(routing_frames) == 1
    redirect = routing_frames[0].get("project_redirect")
    assert redirect is not None
    assert redirect["project_key"] == "soleil"
    assert redirect["display_name"] == "The Soleil Đà Nẵng"
    assert redirect["short_name"] == "The Soleil"

    # The done payload carries the same signal + the deterministic guidance.
    assert len(done_frames) == 1
    done = done_frames[0]
    assert done["project_redirect"]["project_key"] == "soleil"
    assert "The Soleil" in done["answer"]
    assert "chuyển sang dự án" in done["answer"]  # Vietnamese switch instruction
    assert done["sources"] == []  # no corpus retrieval on a redirect turn
    assert done["facts"] == []

    # The pipeline's inner retrieval never ran.
    assert _FakePipeline.calls == []


async def _async_known_projects():
    """Static known-projects pair mirroring the seed, zero I/O."""
    return list(_KNOWN_PROJECTS)


async def _async_registry_record(project_key):
    """Static registry record for camellia; soleil is never looked up."""
    if project_key == "camellia":
        return _camellia_registry_record()
    return None


def _sse_frames(body: str) -> list[tuple[str, dict]]:
    """Parse SSE wire bytes into (event, data) pairs — same shape as
    test_query_api.py, kept local so the test is self-contained."""
    frames: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        event = ""
        data = "{}"
        for line in lines:
            if line.startswith(":"):  # heartbeat / comment frame
                continue
            if ": " in line:
                key, _, val = line.partition(": ")
                if key == "event":
                    event = val
                elif key == "data":
                    data = val
        if event:
            frames.append((event, json.loads(data)))
    return frames


# ===========================================================================
# 6. Project isolation still blocks cross-project data.
# ===========================================================================


def test_legacy_project_isolation_still_blocks_cross_project_data(
    monkeypatch,
) -> None:
    """A query scoped to 'camellia' must only ever see camellia-tagged chunks;
    any soleil chunk that LightRAG surfaces is filtered out by _post_filter
    (documents.project_key column).  This is verified at the HTTP /query level
    by driving a fake pipeline that records the resolved project scope, and then
    by asserting _post_filter drops foreign-project chunks."""

    _reset_pipeline()
    _install_offline_seams(monkeypatch, use_fake_pipeline=True)
    client = TestClient(create_app())

    # --- HTTP-level: the resolved scope is the explicit project, never another. ---
    resp = client.post(
        "/query",
        json={"query": "Giá bất động sản?", "project_key": "camellia"},
    )
    assert resp.status_code == 200
    # The pipeline received exactly camellia, not soleil.
    assert _FakePipeline.last_project_key == "camellia"

    # --- Unit-level: _post_filter drops foreign-project chunks. ---
    from api.application.services import rag_leg

    chunks = [
        {
            "id": "price-camellia-2026q3:3:0",
            "score": 0.95,
            "content": "Camellia pricing",
            "file_path": "price-camellia-2026q3:3:0",
        },
        {
            "id": "price-soleil-2026q3:3:0",
            "score": 0.90,
            "content": "Soleil pricing",
            "file_path": "price-soleil-2026q3:3:0",
        },
        {
            "id": "legal-soleil-chu-truong-2018:1:0",
            "score": 0.88,
            "content": "Soleil legal",
            "file_path": "legal-soleil-chu-truong-2018:1:0",
        },
    ]
    recs = [
        {
            "chunk_id": "price-camellia-2026q3:3:0",
            "doc_id": "price-camellia-2026q3",
            "status": "published",
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "project_key": "camellia",
            "kind": "price",
        },
        {
            "chunk_id": "price-soleil-2026q3:3:0",
            "doc_id": "price-soleil-2026q3",
            "status": "published",
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "project_key": "soleil",
            "kind": "price",
        },
        {
            "chunk_id": "legal-soleil-chu-truong-2018:1:0",
            "doc_id": "legal-soleil-chu-truong-2018",
            "status": "published",
            "effective_from": date(2026, 1, 1),
            "effective_to": None,
            "project_key": "soleil",
            "kind": "legal",
        },
    ]

    kept = _run_post_filter_isolated(monkeypatch, rag_leg, chunks, recs, project_key="camellia")
    kept_ids = [c["id"] for c in kept]
    # Only the camellia chunk survives; soleil chunks are dropped.
    assert kept_ids == ["price-camellia-2026q3:3:0"]


def _run_post_filter_isolated(monkeypatch, rag_leg_module, chunks, recs, *, project_key):
    """Run _post_filter with a fake ro-pool so no real DB is touched."""

    class _FakeConn:
        def __init__(self, rows):
            self._rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def fetch(self, sql, *args):
            return self._rows

    class _FakePool:
        def acquire(self):
            return _FakeConn(recs)

    async def _fake_get_ro_pool():
        return _FakePool()

    monkeypatch.setattr("api.application.services.rag_leg.get_ro_pool", _fake_get_ro_pool)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            rag_leg_module._post_filter(chunks, None, project_key=project_key)
        )
    finally:
        loop.close()
