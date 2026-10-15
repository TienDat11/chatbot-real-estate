"""Regression tests for /query history handling.

FE replays the last 8 messages with full content, and assistant RAG answers
can exceed 2000 chars. The schema must accept long history turns (cap is
DoS-only), and the server must truncate them to MAX_QUERY_LENGTH and drop
blank turns before the pipeline runs — on both the JSON and SSE paths.
"""

from __future__ import annotations

import inspect

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
from tests._training_seams import (
    FakeTrainingChatRepository,
    TRAINING_TEST_UID,
    install_training_seams,
    training_payload,
)


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

    # A request carries one immutable registry snapshot into scope resolution.
    from api.application.ports.project_registry import ProjectRegistryRecord

    async def _fetch_records():
        return [
            ProjectRegistryRecord(
                project_key=key, ten_thuong_mai=name, ten_phap_ly=None,
                vi_tri=None, hotline=None, location=None, geo_center_lat=None,
                geo_center_lng=None, is_hot=False, status="active",
            )
            for key, name in (("camellia", "The Camellia Son Tra - Da Nang"), ("soleil", "The Soleil Đà Nẵng"))
        ]

    monkeypatch.setattr(
        "api.application.services.project_scope.fetch_active_project_records",
        _fetch_records,
    )
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
    assert captured["project_context"].project_key == "camellia"
    assert captured["project_context"].answer_mode == "training"
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

def test_query_json_and_sse_use_validated_project_context(client, monkeypatch):
    from api.application.ports.project_registry import ProjectRegistryRecord
    from api.application.services.project_context import ProjectContext

    active_projects = [
        ProjectRegistryRecord(project_key="camellia", ten_thuong_mai="Camellia", ten_phap_ly=None, vi_tri=None, hotline=None, location="Da Nang", geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active"),
        ProjectRegistryRecord(project_key="soleil", ten_thuong_mai="Soleil", ten_phap_ly=None, vi_tri=None, hotline=None, location="Da Nang", geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active"),
    ]
    expected_context = ProjectContext(
        project_key="soleil",
        answer_mode="normal",
        active_catalogue=(("camellia", "Camellia"), ("soleil", "Soleil")),
    )
    fetched_catalogues = []
    contexts = []

    async def fake_fetch_active_project_records():
        fetched_catalogues.append(active_projects)
        return active_projects

    async def fake_resolve_project_key(requested_key, *, active_projects):
        assert requested_key == "soleil"
        return "soleil"


    # STRICT: legacy /query is unbounded, so the transport forwards no budget.
    async def run(self, query, session_id, as_of, history, *, project_context, device_id=None, on_event=None):
        assert isinstance(project_context, ProjectContext)
        assert project_context == expected_context
        assert project_context.active_catalogue == tuple(
            (project.project_key, project.ten_thuong_mai)
            for project in fetched_catalogues[0]
        )
        contexts.append(project_context)
        return {**_PAYLOAD, "answer": "scoped answer"}

    class Pipeline:
        pass

    Pipeline.run = run
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", Pipeline
    )
    monkeypatch.setattr(
        "api.application.services.project_scope.fetch_active_project_records",
        fake_fetch_active_project_records,
    )
    for headers in ({}, {"Accept": "text/event-stream"}):
        response = client.post(
            "/query", json={"query": "gia can", "project_key": "soleil"}, headers=headers
        )
        assert response.status_code == 200
        if headers:
            done = _sse_frames(response.text)[-1][1]
            assert done["answer"] == "scoped answer"
        else:
            assert response.json()["answer"] == "scoped answer"
    assert len(contexts) == 2
    assert contexts[0] is not contexts[1]
    assert len(fetched_catalogues) == 2
    assert all(catalogue is active_projects for catalogue in fetched_catalogues)
    assert contexts[0].active_catalogue == contexts[1].active_catalogue


def test_legacy_query_is_unbounded_and_sse_matches_json(client, monkeypatch):
    """P0-05: `/query` has NO visible budget — E2 keeps the public request
    contract unchanged, so a >2000-char answer is delivered whole on both
    channels and the SSE token bytes equal the JSON body exactly.

    The bound is proved on the GA SSE generator in
    tests/test_ga_stream_termination.py; this test pins the legacy side.
    """
    calls: list[str] = []
    long_answer = "Camellia có vườn cây xanh. " * 200
    assert len(long_answer) > 2000

    async def run(self, query, session_id, as_of, history, *, project_context,
                  device_id=None, on_event=None):
        # STRICT signature on purpose: the legacy transport is unbounded and
        # must forward NO `visible_limit` on either channel. A regression that
        # passes the keyword raises TypeError and 500s this route.
        calls.append(query)
        # The real pipeline streams one `token` frame for the answer.
        if on_event is not None:
            emitted = on_event("token", {"text": long_answer})
            if inspect.isawaitable(emitted):
                await emitted
        return {**_PAYLOAD, "answer": long_answer}

    class Pipeline:
        pass

    Pipeline.run = run
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", Pipeline
    )

    body = client.post("/query", json={"query": "tien ich noi bo", "project_key": "soleil"})
    assert body.status_code == 200
    assert body.json()["answer"] == long_answer

    sse = client.post(
        "/query", json={"query": "tien ich noi bo", "project_key": "soleil"},
        headers={"Accept": "text/event-stream"},
    )
    frames = _sse_frames(sse.text)
    streamed = "".join(
        data.get("text", "") for name, data in frames if name == "token"
    )
    done = frames[-1]
    assert done[0] == "done" and done[1]["answer"] == long_answer
    assert streamed.encode("utf-8") == long_answer.encode("utf-8")
    # Both channels reached the strict fake, so neither forwarded a bound.
    assert calls == ["tien ich noi bo", "tien ich noi bo"], calls


def test_query_self_mention_uses_resolved_single_project(client, monkeypatch):
    from api.application.ports.project_registry import ProjectRegistryRecord
    from api.application.services.project_context import ProjectContext

    active_projects = [ProjectRegistryRecord(project_key="camellia", ten_thuong_mai="Camellia", ten_phap_ly=None, vi_tri=None, hotline=None, location="Da Nang", geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active")]
    contexts = []

    async def fake_fetch_active_project_records():
        return active_projects
    async def fake_resolve_project_key(requested_key, *, active_projects):
        assert requested_key is None
        return "camellia"

    monkeypatch.setattr(
        "api.application.services.project_scope.fetch_active_project_records",
        fake_fetch_active_project_records,
    )
    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key",
        fake_resolve_project_key,
    )

    async def run(self, query, session_id, as_of, history, **kwargs):
        context = kwargs["project_context"]
        assert isinstance(context, ProjectContext)
        assert context.project_key == "camellia"
        contexts.append(context)
        return {**_PAYLOAD, "answer": "scoped answer"}

    class Pipeline:
        pass

    Pipeline.run = run
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", Pipeline
    )
    response = client.post("/query", json={"query": "Bạn biết gì về Camellia không?"})

    assert response.status_code == 200
    assert response.json()["answer"] == "scoped answer"
    assert response.json().get("project_redirect") is None
    assert len(contexts) == 1


def test_query_foreign_project_redirects_before_quota(client, monkeypatch):
    from api.application.services.query_quota_gate import UnmanagedTurnContext

    calls = []

    class Gate:
        async def prepare_turn(self, **kwargs):
            calls.append("quota")
            return UnmanagedTurnContext()

    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate", Gate
    )
    response = client.post(
        "/query",
        json={"query": "Bạn biết gì về soleil không?", "project_key": "camellia"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["project_redirect"]["project_key"] == "soleil"
    assert "The Soleil" in body["answer"]
    assert calls == []


def test_query_json_preserves_project_context_to_inner_workflow(monkeypatch):
    from functools import wraps
    from uuid import uuid4

    from api.application.pipelines import conv_workflow, workflow
    from api.application.ports.project_registry import ProjectRegistryRecord
    from api.application.services import project_scope
    from api.application.services.project_context import ProjectContext
    from api.application.services.project_scope import ActiveProject
    from api.application.services.query_quota_gate import UnmanagedTurnContext
    from api.infrastructure.adapters.postgres_chat_history import repository
    from api.interfaces.api.main import create_app

    active_projects = [
        ActiveProject("camellia", "Camellia"),
        ActiveProject("soleil", "Soleil"),
    ]
    catalogue = (("camellia", "Camellia"), ("soleil", "Soleil"))
    registry_record = ProjectRegistryRecord(
        project_key="soleil",
        ten_thuong_mai="Soleil",
        ten_phap_ly=None,
        vi_tri=None,
        hotline=None,
        location=None,
        geo_center_lat=None,
        geo_center_lng=None,
        is_hot=False,
        status="active",
    )
    contexts = []
    active_fetches = []
    resolver_calls = []
    facade_contexts = []
    conv_events = []
    inner_contexts = []
    guard_context_writes = []
    registry_lookups = []
    quota_calls = []
    persistence_calls = []
    rag_project_keys = []

    original_post_init = ProjectContext.__post_init__

    def observe_context_init(self):
        original_post_init(self)
        contexts.append(self)

    monkeypatch.setattr(ProjectContext, "__post_init__", observe_context_init)

    async def fetch_active_project_records():
        active_fetches.append(active_projects)
        return [ProjectRegistryRecord(project_key="camellia", ten_thuong_mai="Camellia", ten_phap_ly=None, vi_tri=None, hotline=None, location=None, geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active"), registry_record]

    original_resolve = project_scope.resolve_project_key

    async def observe_resolve(requested, *, active_projects=None):
        result = await original_resolve(requested, active_projects=active_projects)

        resolver_calls.append((requested, active_projects, result))
        return result
    monkeypatch.setattr(project_scope, "fetch_active_project_records", fetch_active_project_records)
    monkeypatch.setattr(project_scope, "resolve_project_key", observe_resolve)

    async def load_registry(project_key):
        registry_lookups.append(project_key)
        return registry_record

    monkeypatch.setattr(
        "api.application.services.project_config.load_project_registry_record",
        load_registry,
    )

    class CountingQuotaGate:
        async def prepare_turn(self, **kwargs):
            quota_calls.append(kwargs)
            return UnmanagedTurnContext()

        async def refund_turn(self, context):
            return None

    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: CountingQuotaGate(),
    )

    async def append_turn(**kwargs):
        persistence_calls.append(kwargs)
        return True

    monkeypatch.setattr(repository, "append_turn", append_turn)

    original_facade_run = conv_workflow.RagQueryPipelineConv.run

    @wraps(original_facade_run)
    async def observe_facade_run(self, *args, **kwargs):
        facade_contexts.append(kwargs["project_context"])
        return await original_facade_run(self, *args, **kwargs)

    monkeypatch.setattr(conv_workflow.RagQueryPipelineConv, "run", observe_facade_run)

    original_conv_event_init = conv_workflow.ConvRunEv.__init__

    def observe_conv_event_init(self, *args, **kwargs):
        original_conv_event_init(self, *args, **kwargs)
        conv_events.append(self)

    monkeypatch.setattr(conv_workflow.ConvRunEv, "__init__", observe_conv_event_init)

    original_inner_run = workflow.RagQueryWorkflow.run

    def observe_inner_run(self, *args, **kwargs):
        inner_contexts.append(kwargs["project_context"])
        return original_inner_run(self, *args, **kwargs)

    monkeypatch.setattr(workflow.RagQueryWorkflow, "run", observe_inner_run)

    original_guard = workflow.RagQueryWorkflow.guard

    @wraps(original_guard)
    async def observe_guard(self, ctx, ev):
        store_type = type(ctx.store)
        original_set = store_type.set

        async def observe_set(store, key, value):
            result = await original_set(store, key, value)
            if key == "project_context":
                guard_context_writes.append(value)
            return result

        monkeypatch.setattr(store_type, "set", observe_set)
        return await original_guard(self, ctx, ev)

    monkeypatch.setattr(workflow.RagQueryWorkflow, "guard", observe_guard)

    async def rewrite_query(query, history, as_of):
        return workflow.RoutedResult(
            rewritten=query,
            routing={
                "needs_rag": True,
                "needs_sql": False,
                "needs_geo": False,
                "structured_path": "none",
                "high_stakes": False,
            },
            sql_spec=None,
            hl_keywords=[],
            ll_keywords=[],
            high_stakes=False,
            as_of=as_of,
        )

    async def empty_rag_leg(rewritten, hl, ll, as_of, project_key=None):
        rag_project_keys.append(project_key)
        return workflow.RagLegResult([], degraded=False)

    class OfflineReranker:
        async def rerank(self, query, chunks):
            return chunks

    async def no_images(*args, **kwargs):
        return []

    async def offline_answer(*args, **kwargs):
        yield "offline answer"

    async def no_audit(*args, **kwargs):
        return None

    async def no_pg2(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(workflow, "rewrite_query", rewrite_query)
    monkeypatch.setattr(workflow, "run_rag_leg", empty_rag_leg)
    monkeypatch.setattr(workflow, "get_reranker", lambda: OfflineReranker())
    monkeypatch.setattr(workflow, "search_images", no_images)
    monkeypatch.setattr(workflow, "stream_answer", offline_answer)
    monkeypatch.setattr(workflow, "write_audit", no_audit)
    monkeypatch.setattr("api.domain.services.guard_input._pg2_check", no_pg2)

    session_id = f"query-bridge-{uuid4().hex}"
    standalone_client = TestClient(create_app())
    response = standalone_client.post(
        "/query",
        json={
            "query": "thông tin căn hộ",
            "project_key": "soleil",
            "session_id": session_id,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body.get("project_redirect") is None
    assert body["history_persisted"] is True

    assert len(active_fetches) == 1
    assert active_fetches[0] is active_projects
    assert len(resolver_calls) == 1
    assert resolver_calls[0][0] == "soleil"
    assert [(project.project_key, project.ten_thuong_mai) for project in resolver_calls[0][1]] == list(catalogue)
    assert resolver_calls[0][2] == "soleil"
    assert len(contexts) == 1
    context = contexts[0]
    assert context.project_key == "soleil"
    assert context.answer_mode == "normal"
    assert context.active_catalogue == catalogue

    assert len(facade_contexts) == 1
    assert facade_contexts[0] is context
    assert len(conv_events) == 1
    assert conv_events[0].project_context is context
    assert len(inner_contexts) == 1
    assert inner_contexts[0] is context
    assert len(guard_context_writes) == 1
    assert guard_context_writes[0] is context
    assert rag_project_keys == ["soleil"]
    assert registry_lookups == ["soleil"]
    assert len(quota_calls) == 1
    assert quota_calls[0]["project_key"] == "soleil"
    assert len(persistence_calls) == 1
    assert persistence_calls[0]["project_key"] == "soleil"

def test_query_training_preserves_project_context_to_inner_guard(
    monkeypatch, offline_auth_seams, local_rsa_jwk
):
    from functools import wraps

    from api.application.pipelines import conv_workflow, workflow
    from api.application.services import project_config, project_scope
    from api.application.services.project_context import ProjectContext
    from api.application.services.project_scope import ActiveProject
    from api.application.ports.project_registry import ProjectRegistryRecord
    from api.application.services.query_quota_gate import UnmanagedTurnContext
    from api.domain.services import guard_input
    from api.interfaces.api import main as main_api
    bind_results = []


    repo = FakeTrainingChatRepository()
    install_training_seams(monkeypatch, repo=repo)
    registry_record = ProjectRegistryRecord(
        project_key="camellia",
        ten_thuong_mai="Camellia",
        ten_phap_ly=None,
        vi_tri=None,
        hotline=None,
        location=None,
        geo_center_lat=None,
        geo_center_lng=None,
        is_hot=False,
        status="active",
    )
    original_registry_lookup = project_config.load_project_registry_record
    active_projects = [ActiveProject("camellia", "Camellia"), ActiveProject("soleil", "Soleil")]
    catalogue = (("camellia", "Camellia"), ("soleil", "Soleil"))
    events = []
    contexts = []
    facade_contexts = []
    conv_events = []
    inner_contexts = []
    rag_contexts = []
    guard_context_writes = []
    active_fetches = []
    resolver_calls = []
    registry_lookups = []
    quota_calls = []

    original_post_init = ProjectContext.__post_init__

    def observe_context_init(self):
        original_post_init(self)
        contexts.append(self)

    monkeypatch.setattr(ProjectContext, "__post_init__", observe_context_init)

    async def fetch_active_project_records():
        events.append("active_catalogue_fetch")
        active_fetches.append(active_projects)
        return [registry_record, ProjectRegistryRecord(project_key="soleil", ten_thuong_mai="Soleil", ten_phap_ly=None, vi_tri=None, hotline=None, location=None, geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active")]

    original_resolve = project_scope.resolve_project_key

    async def observe_resolve(requested, *, active_projects=None):
        resolver_calls.append(requested)
        return await original_resolve(requested, active_projects=active_projects)

    monkeypatch.setattr(project_scope, "fetch_active_project_records", fetch_active_project_records)
    monkeypatch.setattr(project_scope, "resolve_project_key", observe_resolve)

    async def observe_registry_lookup(project_key):
        registry_lookups.append(project_key)
        events.append(
            "training_project_preflight"
            if len(registry_lookups) == 1
            else "facade_registry_lookup"
        )
        return registry_record if project_key == "camellia" else await original_registry_lookup(project_key)

    monkeypatch.setattr(project_config, "load_project_registry_record", observe_registry_lookup)

    original_preflight = main_api._validate_training_request

    @wraps(original_preflight)
    async def observe_training_preflight(req, principal):
        context_key = await original_preflight(req, principal)
        events.append("training_preflight_complete")
        return context_key

    monkeypatch.setattr(main_api, "_validate_training_request", observe_training_preflight)

    original_session_lookup = repo.get_training_session
    original_session_bind = repo.bind_training_session

    async def observe_session_lookup(**kwargs):
        events.append("session_lookup")
        return await original_session_lookup(**kwargs)

    async def observe_session_bind(**kwargs):
        result = await original_session_bind(**kwargs)
        bind_results.append(result)
        events.append("session_bind")
        return result

    monkeypatch.setattr(repo, "get_training_session", observe_session_lookup)
    monkeypatch.setattr(repo, "bind_training_session", observe_session_bind)

    class CountingQuotaGate:
        async def prepare_turn(self, **kwargs):
            events.append("quota")
            quota_calls.append(kwargs)
            return UnmanagedTurnContext()

        async def refund_turn(self, context):
            return None

    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: CountingQuotaGate(),
    )

    original_facade_run = conv_workflow.RagQueryPipelineConv.run

    @wraps(original_facade_run)
    async def observe_facade_run(self, *args, **kwargs):
        facade_contexts.append(kwargs["project_context"])
        return await original_facade_run(self, *args, **kwargs)

    monkeypatch.setattr(conv_workflow.RagQueryPipelineConv, "run", observe_facade_run)

    original_conv_event_init = conv_workflow.ConvRunEv.__init__

    def observe_conv_event_init(self, *args, **kwargs):
        original_conv_event_init(self, *args, **kwargs)
        conv_events.append(self)

    monkeypatch.setattr(conv_workflow.ConvRunEv, "__init__", observe_conv_event_init)

    original_inner_run = workflow.RagQueryWorkflow.run

    def observe_inner_run(self, *args, **kwargs):
        inner_contexts.append(kwargs["project_context"])
        return original_inner_run(self, *args, **kwargs)

    monkeypatch.setattr(workflow.RagQueryWorkflow, "run", observe_inner_run)

    original_rag_leg = workflow.RagQueryWorkflow.rag_leg

    @wraps(original_rag_leg)
    async def observe_rag_leg(self, ctx, ev):
        rag_contexts.append(await ctx.store.get("project_context"))
        return await original_rag_leg(self, ctx, ev)

    monkeypatch.setattr(workflow.RagQueryWorkflow, "rag_leg", observe_rag_leg)

    original_guard = workflow.RagQueryWorkflow.guard

    @wraps(original_guard)
    async def observe_guard(self, ctx, ev):
        store_type = type(ctx.store)
        original_set = store_type.set

        async def observe_set(store, key, value):
            result = await original_set(store, key, value)
            if key == "project_context":
                guard_context_writes.append(value)
            return result

        monkeypatch.setattr(store_type, "set", observe_set)
        return await original_guard(self, ctx, ev)

    monkeypatch.setattr(workflow.RagQueryWorkflow, "guard", observe_guard)

    async def rewrite_query(query, history, as_of):
        return workflow.RoutedResult(
            rewritten=query,
            routing={
                "needs_rag": True,
                "needs_sql": False,
                "needs_geo": False,
                "structured_path": "none",
                "high_stakes": False,
            },
            sql_spec=None,
            hl_keywords=[],
            ll_keywords=[],
            high_stakes=False,
            as_of=as_of,
        )

    async def empty_rag_leg(rewritten, hl, ll, as_of, project_key=None):
        rag_project_keys.append(project_key)
        return workflow.RagLegResult([], degraded=False)

    rag_project_keys = []

    class OfflineReranker:
        async def rerank(self, query, chunks):
            return chunks

    async def no_images(*args, **kwargs):
        return []

    async def offline_answer(*args, **kwargs):
        yield "offline training answer"

    async def no_audit(*args, **kwargs):
        return None

    async def no_pg2(*args, **kwargs):
        return 0.0

    monkeypatch.setattr(workflow, "rewrite_query", rewrite_query)
    monkeypatch.setattr(workflow, "run_rag_leg", empty_rag_leg)
    monkeypatch.setattr(workflow, "get_reranker", lambda: OfflineReranker())
    monkeypatch.setattr(workflow, "search_images", no_images)
    monkeypatch.setattr(workflow, "stream_answer", offline_answer)
    monkeypatch.setattr(workflow, "write_audit", no_audit)
    monkeypatch.setattr(guard_input, "_pg2_check", no_pg2)

    session_id = "s-training-1"
    response = TestClient(main_api.create_app()).post(
        "/query",
        json=training_payload(query="coaching", session_id=session_id),
        headers=sales_bearer_headers(local_rsa_jwk),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["history_persisted"] is True
    assert body["training_session_id"] == session_id
    assert body["context"] == {"project_key": "camellia"}
    assert body["lead_cta_hint"] is None
    assert body["project_redirect"] is None

    assert events == [
        "training_project_preflight",
        "session_lookup",
        "session_bind",
        "training_preflight_complete",
        "active_catalogue_fetch",
        "quota",
        "facade_registry_lookup",
    ]
    assert bind_results == [True]
    assert len(active_fetches) == 1
    assert active_fetches[0] is active_projects
    assert resolver_calls == []
    assert len(contexts) == 1
    context = contexts[0]
    assert context.project_key == "camellia"
    assert context.answer_mode == "training"
    assert context.active_catalogue == catalogue

    assert facade_contexts == [context]
    assert len(conv_events) == 1
    assert conv_events[0].project_context is context
    assert inner_contexts == [context]
    assert rag_contexts == [context]
    assert rag_contexts[0].project_key == "camellia"
    assert rag_contexts[0].answer_mode == "training"
    assert guard_context_writes == [context]
    assert guard_context_writes[0].answer_mode == "training"
    assert rag_project_keys == ["camellia"]

    assert registry_lookups == ["camellia", "camellia"]
    assert events.count("training_project_preflight") == 1
    assert events.count("facade_registry_lookup") == 1
    assert len(quota_calls) == 1
    assert quota_calls[0]["project_key"] == "camellia"

    assert repo.bound == {session_id: (TRAINING_TEST_UID, "camellia")}
    assert len(repo.training_turns) == 1
    turn = repo.training_turns[0]
    assert turn["session_id"] == session_id
    assert turn["owner_firebase_uid"] == TRAINING_TEST_UID
    assert turn["context_project_key"] == "camellia"
    assert repo.customer_append_calls == 0
