"""HTTP-level regression for the GA-06 transport adapter contract.

Proves that the private ``source_event_key`` is rejected at FastAPI
validation (``422``) — never accepted into ``GlobalAssistantInput`` — for
both the JSON and SSE routes, and that no side effects fire when validation
fails (no pipeline, no quota reservation, no persistence).

Also pins the HTTP success contract for POST /api/assistant/query: a resolved
request returns 200 with the full observable outcome (pipeline once, quota
prepare+finalize once, refund 0, persistence once, no private key on the wire),
and an unresolved request returns 200 with no pipeline/quota and clarification
candidates.

DB-free: every I/O seam is overridden; the real Postgres adapter and the
real quota gate factory are swapped for counting fakes, and ``_service()``
is monkeypatched so the real GlobalAssistantService core runs through the HTTP
surface with injectable seams.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.interfaces.api.assistant_routes as assistant_routes

ANON_TOKEN = "x-anon-token-header"
DEVICE_ID = "test-device-001"


class _CountingPipeline:
    """Pipeline factory fake that records construction + run calls.

    Mirrors the real factory signature ``factory(on_event=None) -> obj.run(...)``
    so the real GlobalAssistantService core can drive it through execute_prepared_turn.
    """

    def __init__(self, answer="42 tỷ cho căn A1-401."):
        self.instances = 0
        self.ran = 0
        self.answer = answer

    def __call__(self, on_event=None):
        self.instances += 1

        outer = self

        class _Pipeline:
            async def run(self, *args, **kwargs):
                outer.ran += 1
                return {
                    "answer": outer.answer,
                    "trace_id": "t-contract",
                    "project_redirect": {"project_key": "camellia"},
                    "sources": [{"doc_id": "d1"}],
                }

        return _Pipeline()


class _AnonymousTurnContext:
    """Stand-in for AnonymousTurnContext: finite reservation present.

    TRAP A: ``_reservation_managed`` keys on ``reserved_snapshot``; without it
    the route skips finalize and the finalize-once assertion passes vacuously.
    This mirrors AnonymousTurnContext's shape (identity_key, project_key,
    reserved_snapshot) so the managed path is genuinely exercised.
    """

    def __init__(self, project_key: str = "camellia") -> None:
        self.identity_key = "anon:test"
        self.project_key = project_key
        self.reserved_snapshot = object()


class _CountingQuotaGate:
    """Quota gate fake that records prepare/finalize/refund.

    Called as ``quota_gate()`` (factory) AND ``quota_gate.prepare_turn(...)`` —
    the route depends on ``build_query_quota_gate`` and calls it, then invokes
    prepare_turn/finalize_turn/refund_turn on the returned instance.
    """

    def __init__(self) -> None:
        self.instances = 0
        self.prepared = 0
        self.finalized = 0
        self.refunded = 0

    def __call__(self):
        self.instances += 1
        return self

    async def prepare_turn(self, **kwargs):
        self.prepared += 1
        return _AnonymousTurnContext(project_key=kwargs.get("project_key", "camellia"))

    async def finalize_turn(self, context=None):
        self.finalized += 1

    async def refund_turn(self, context=None):
        self.refunded += 1


class _CountingHistory:
    """Stateful fake ChatHistoryService: counts persistence, returns a session.

    TRAP B: the previous _FakeHistory was no-op (empty messages, no counter),
    so the route could not prove the core actually persisted. This fake counts
    persist_global_turn calls and returns a session with an active project so
    the real core's resolve/persist flow is driven end-to-end.
    """

    def __init__(self) -> None:
        self.persisted = 0
        self.appended: list[dict] = []

    async def global_session(self, *, session_id, device_id=None, identity_key=None):
        return type("_S", (), {"active_project_key": None})()

    async def global_messages(self, *, session_id, device_id=None, identity_key=None):
        return []

    async def persist_global_turn(self, **kwargs):
        self.persisted += 1
        self.appended.append(kwargs)
        return True


class _FakeIdentityService:
    """Identity service that always verifies the token (anonymous flow)."""

    def __init__(self, claims_subject: str = "anon-sub") -> None:
        self.claims_subject = claims_subject
        self.verified = 0

    def verify_token(self, token: str):
        self.verified += 1
        from api.application.services.anon_identity import AnonymousIdentityClaims

        return AnonymousIdentityClaims(
            subject=self.claims_subject,
            issued_at_epoch_seconds=1_000_000_000,
            token_version=1,
        )


async def _catalogue_camellia():
    return [("camellia", "Camellia")]


@pytest.fixture
def app(monkeypatch):
    """Thin FastAPI app with only the assistant router and all seams swapped."""
    from api.interfaces.api.anon_routes import get_anonymous_identity_service
    from api.interfaces.api.deps import optional_sales_or_admin

    pipeline = _CountingPipeline()
    quota = _CountingQuotaGate()
    identity = _FakeIdentityService()
    history = _CountingHistory()
    service_calls = {"count": 0}

    def _fake_service():
        service_calls["count"] += 1
        from api.application.services.global_assistant_service import (
            GlobalAssistantService,
        )

        return GlobalAssistantService(
            history=history,
            pipeline_factory=pipeline,
            catalogue_loader=_catalogue_camellia,
        )

    test_app = FastAPI()
    test_app.include_router(assistant_routes.router)
    test_app.dependency_overrides[optional_sales_or_admin] = lambda: None
    test_app.dependency_overrides[get_anonymous_identity_service] = lambda: identity
    # Route both the JSON and SSE paths through the fake core (verified: both
    # handlers call _service() in assistant_routes.py).
    monkeypatch.setattr(assistant_routes, "_service", _fake_service)
    monkeypatch.setattr(assistant_routes, "build_query_quota_gate", quota)

    test_app._test_state = {
        "pipeline": pipeline,
        "quota": quota,
        "identity": identity,
        "history": history,
        "service_calls": service_calls,
    }
    return test_app


class _FakeHistory:
    """No-op ChatHistoryService stand-in so _service() can build a core.

    Retained only for tests that exercise rejection before the core is built
    (422 validation). The real counting fake lives on ``app._test_state``.
    """

    async def global_session(self, *, session_id, device_id=None, identity_key=None):
        return None

    async def global_messages(self, *, session_id, device_id=None, identity_key=None):
        return []

    async def persist_global_turn(self, **kwargs):
        return True


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    ["/api/assistant/query", "/api/assistant/query/stream"],
)
def test_source_event_key_rejected_at_validation(app, path):
    """``source_event_key`` is private: FastAPI must reject it with 422
    before the handler body runs, so no core, quota, or pipeline side effects
    occur on either transport."""
    client = _client(app)

    response = client.post(
        path,
        json={"query": "Camellia giá bao nhiêu?", "source_event_key": "attacker-controlled"},
        headers={"X-Anon-Token": ANON_TOKEN, "X-Device-Id": DEVICE_ID},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    # Pydantic v2 validation error shape: detail -> list of loc entries.
    rejected = False
    for err in body.get("detail", []):
        loc = err.get("loc", [])
        if "source_event_key" in loc:
            rejected = True
            break
    assert rejected, body


def test_source_event_key_rejection_has_zero_side_effects(app):
    """422 must short-circuit ALL the way: no core, no quota, no pipeline."""
    client = _client(app)
    state = app._test_state

    response = client.post(
        "/api/assistant/query",
        json={"query": "Camellia giá bao nhiêu?", "source_event_key": "attacker-controlled"},
        headers={"X-Anon-Token": ANON_TOKEN, "X-Device-Id": DEVICE_ID},
    )

    assert response.status_code == 422
    assert state["service_calls"]["count"] == 0, "core service must not be built"
    assert state["quota"].instances == 0, "quota gate must not be built"
    assert state["quota"].prepared == 0, "quota.prepare_turn must not run"
    assert state["pipeline"].instances == 0, "pipeline must not be constructed"
    assert state["identity"].verified == 0, "identity verification must not run"


def test_source_event_key_rejection_sse_zero_side_effects(app):
    """Same zero-side-effect guarantee for the streaming route."""
    client = _client(app)
    state = app._test_state

    response = client.post(
        "/api/assistant/query/stream",
        json={"query": "Camellia giá bao nhiêu?", "source_event_key": "attacker-controlled"},
        headers={"X-Anon-Token": ANON_TOKEN, "X-Device-Id": DEVICE_ID},
    )

    assert response.status_code == 422
    assert state["service_calls"]["count"] == 0
    assert state["quota"].instances == 0
    assert state["pipeline"].instances == 0


def test_resolved_http_success_contract(app):
    """POST /api/assistant/query {"query": "Camellia giá bao nhiêu?"} -> full
    success contract through the real core + counting fakes.

    Asserts what the REAL core does through the HTTP surface: exact 200,
    resolution_status == resolved, project_key == camellia, pipeline run
    exactly once, quota prepared + finalized exactly once, refund 0,
    persistence exactly once, and the private _source_event_key never appears
    in the response body text.
    """
    client = _client(app)
    state = app._test_state
    pipeline = state["pipeline"]
    quota = state["quota"]
    history = state["history"]

    response = client.post(
        "/api/assistant/query",
        json={"query": "Camellia giá bao nhiêu?"},
        headers={"X-Anon-Token": ANON_TOKEN, "X-Device-Id": DEVICE_ID},
    )

    # Exact success contract (not "!= 422").
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution_status"] == "resolved"
    assert body["project_key"] == "camellia"
    assert body["rag_ran"] is True

    # Pipeline constructed and run exactly once.
    assert pipeline.instances == 1, "pipeline factory must be called once"
    assert pipeline.ran == 1, "pipeline.run must be called once"

    # Quota reserved after resolution (spec §5.3), finalized on success, never refunded.
    assert quota.prepared == 1
    assert quota.finalized == 1
    assert quota.refunded == 0

    # Persistence exactly once through the real core's _persist_outcome seam.
    assert history.persisted == 1
    assert history.appended[0]["resolved_project_key"] == "camellia"

    # The private key must never appear in the HTTP response body text.
    assert "_source_event_key" not in response.text


def test_unresolved_http_no_pipeline_no_quota(app):
    """POST /api/assistant/query with a project-less query -> 200, no pipeline,
    no quota reservation, clarification candidates returned."""
    client = _client(app)
    state = app._test_state
    pipeline = state["pipeline"]
    quota = state["quota"]
    history = state["history"]

    response = client.post(
        "/api/assistant/query",
        json={"query": "Dự án nào có hồ bơi?"},
        headers={"X-Anon-Token": ANON_TOKEN, "X-Device-Id": DEVICE_ID},
    )

    # Exact success contract for the unresolved path.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution_status"] != "resolved"
    assert body["rag_ran"] is False

    # No pipeline, no quota, but the clarification turn is still persisted.
    assert pipeline.instances == 0
    assert pipeline.ran == 0
    assert quota.prepared == 0
    assert quota.finalized == 0
    assert quota.refunded == 0
    assert history.persisted == 1
    assert history.appended[0]["resolved_project_key"] is None

    # Clarification candidates are surfaced (from the live catalogue fallback
    # for UNRESOLVED) so the client picker can render.
    assert body["candidates"] == ["camellia"]
