"""Cross-project guardrail + /api/projects contract tests (project awareness).

User-reported bug: standing in Camellia and asking "Bạn biết gì về soleil
không?" was answered with Camellia data. Pinned contracts through
RagRgreConvWorkflow (the class actually wired behind POST /query):

1. A question naming another KNOWN project never reaches retrieval: routing
   carries ``project_redirect``, the done payload carries the Vietnamese
   guidance answer plus the same signal, sources/citations are empty by
   construction, and the inner RAG pipeline is never invoked.
2. Current-project mention (or no mention) keeps the normal flow untouched;
   unknown third-party names ('vinhomes') keep it too.
3. Comparison questions naming BOTH projects still redirect.
4. The redirect turn feeds CTA accounting exactly like a clean answer, so
   lead_cta_hint trigger parity across projects is preserved.
5. GET /api/projects serves
   {"projects": [{project_key, display_name, short_name}, ...]}
   with camellia pinned first, soleil second, any others after.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.application.services.anon_identity import AnonymousIdentityService
from api.infrastructure.config.config import get_settings

from api.application.pipelines.conv_workflow import (
    SSE_EVENT_ROUTING,
    RagRgreConvWorkflow,
)
from api.application.ports.project_registry import ProjectRegistryRecord
from api.application.services.conv_state import (
    CTA_VARIANTS,
    get_context,
)
from api.application.services.project_config import project_catalogue_from_records
from api.domain.services.project_redirect import (
    detect_foreign_project,
    normalize_project_text,
    redirect_message,
    short_display_name,
    strip_city_suffix,
)
from api.interfaces.api.main import create_app

# Registry-order pairs mirroring db/seed/project_config.sql (camellia first).
# Soleil's registry ten_thuong_mai carries the long marketing qualifier on
# purpose, to pin that the redirect label strips it down to the short name.
KNOWN_PROJECTS = [
    ("camellia", "The Camellia Son Tra - Da Nang"),
    (
        "soleil",
        "The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite Collection)",
    ),
]


class _RecordingInner:
    """Fake inner RagQueryWorkflow: records invocations, returns one canned result.

    ``calls`` doubles as the "did retrieval run?" oracle for guard tests.
    """

    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self._handler()

    async def _handler(self):
        return dict(self.result)


def _clean_inner_result() -> dict[str, Any]:
    return {
        "answer": "Chính sách bán hàng dự án ...",
        "sources": [{"doc_id": "doc-1", "title": "T"}],
        "facts": [],
        "requires_review": False,
        "confidence": "HIGH",
    }


def _routing_frames(events: list[tuple[str, dict]]) -> list[dict]:
    return [data for event, data in events if event == SSE_EVENT_ROUTING]


@pytest.fixture(autouse=True)
def _hermetic_project_identity(monkeypatch):
    identities = {
        "camellia": {"ten_thuong_mai": "The Camellia Son Tra - Da Nang", "ten_phap_ly": None, "vi_tri": "Da Nang", "hotline": None},
        "soleil": {"ten_thuong_mai": "The Soleil Đà Nẵng", "ten_phap_ly": None, "vi_tri": "Da Nang", "hotline": None},
    }

    def fake_identity(project_key=None):
        return dict(identities.get(project_key or "camellia", identities["camellia"]))

    monkeypatch.setattr("api.application.services.project_config.fetch_project_identity", fake_identity)
    monkeypatch.setattr("api.application.pipelines.conv_workflow.brand_token", lambda key=None: (key or "camellia").lower())
    from api.application.ports.project_registry import ProjectRegistryRecord

    async def fake_active_project_records():
        return [
            ProjectRegistryRecord(
                project_key=key, ten_thuong_mai=name, ten_phap_ly=None,
                vi_tri="Da Nang", hotline=None, location="Da Nang",
                geo_center_lat=None, geo_center_lng=None, is_hot=False, status="active",
            )
            for key, name in KNOWN_PROJECTS
        ]

    async def fake_resolve_project_key(requested, *, active_projects=None):
        return _resolved(requested, active_projects)

    monkeypatch.setattr("api.application.services.project_scope.fetch_active_project_records", fake_active_project_records)
    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", fake_resolve_project_key)


def _active_projects():
    from api.application.services.project_scope import ActiveProject
    return [ActiveProject(key, name) for key, name in KNOWN_PROJECTS]


def _resolved(requested, active_projects):
    return requested or "camellia"

# --- domain: normalization + detection ----------------------------------------


def test_normalize_strips_diacritics_case_and_dashes_spacing() -> None:
    # Query text is folded too, so user diacritics never break matching.
    assert normalize_project_text("The Soleil ĐÀ NẴNG") == "the soleil da nang"
    assert normalize_project_text("  Bạn   biết gì  ") == "ban biet gi"


def test_short_display_name_strips_marketing_qualifier() -> None:
    assert short_display_name(KNOWN_PROJECTS[1][1]) == "The Soleil Đà Nẵng"
    assert short_display_name("The Camellia Son Tra - Da Nang") == "The Camellia Son Tra - Da Nang"
    assert short_display_name(None) == ""


def test_strip_city_suffix_removes_trailing_city_token() -> None:
    # Diacritic spelling (soleil display label) and ASCII seed spelling
    # (camellia ten_thuong_mai) both strip; other names pass through.
    assert strip_city_suffix("The Soleil Đà Nẵng") == "The Soleil"
    assert strip_city_suffix("The Camellia Son Tra - Da Nang") == "The Camellia Son Tra"
    assert strip_city_suffix("The Origami Hà Nội") == "The Origami"
    assert strip_city_suffix("The Tower TP. Hồ Chí Minh") == "The Tower"
    assert strip_city_suffix("The Tower Hồ Chí Minh") == "The Tower"
    # No trailing city token -> unchanged; token-only name never goes empty.
    assert strip_city_suffix("Zeta Heights") == "Zeta Heights"
    assert strip_city_suffix("Đà Nẵng") == "Đà Nẵng"
    assert strip_city_suffix(None) == ""


def test_detect_redirects_foreign_project_with_variants() -> None:
    # Bare key, capitalized, mid-sentence — the exact reported bug phrasing.
    redirect = detect_foreign_project("Bạn biết gì về soleil không?", "camellia", KNOWN_PROJECTS)
    assert redirect == {
        "project_key": "soleil",
        "display_name": "The Soleil Đà Nẵng",
        "short_name": "The Soleil",
    }
    # Full diacritics-bearing commercial name also matches.
    assert (
        detect_foreign_project("the Soleil Đà Nẵng có gì hay?", "camellia", KNOWN_PROJECTS)[
            "project_key"
        ]
        == "soleil"
    )
    # 'The Soleil' prefix variant.
    assert detect_foreign_project("dự án The Soleil thế nào", "camellia", KNOWN_PROJECTS)


def test_detect_keeps_normal_flow_for_current_or_no_mention() -> None:
    assert (
        detect_foreign_project("bảng giá The Camellia bao nhiêu", "camellia", KNOWN_PROJECTS)
        is None
    )
    assert detect_foreign_project("chuẩn bàn giao thế nào ạ", "camellia", KNOWN_PROJECTS) is None


def test_detect_comparison_question_still_redirects() -> None:
    redirect = detect_foreign_project(
        "so sánh camellia với soleil được không", "camellia", KNOWN_PROJECTS
    )
    assert redirect == {
        "project_key": "soleil",
        "display_name": "The Soleil Đà Nẵng",
        "short_name": "The Soleil",
    }


def test_detect_ignores_unknown_third_party_names() -> None:
    assert detect_foreign_project("vinhomes có tốt không vậy", "camellia", KNOWN_PROJECTS) is None


def test_redirect_message_names_full_label_then_short_name() -> None:
    message = redirect_message(KNOWN_PROJECTS[1][1])
    # First mention carries the full display label; the switch instruction
    # uses the compact short name (no trailing city token).
    assert message.count("The Soleil Đà Nẵng") == 1
    assert "chuyển sang dự án The Soleil để em giải đáp" in message


# --- /query pinned-project redirect contract ---------------------------------


def test_foreign_project_redirects_before_quota_and_retrieval(monkeypatch):
    import json
    calls = []

    class Gate:
        async def prepare_turn(self, **kwargs):
            calls.append("quota")
            raise AssertionError("redirect must precede quota")

    monkeypatch.setattr("api.application.services.query_quota_gate.build_query_quota_gate", Gate)
    with TestClient(create_app()).stream(
        "POST", "/query", json={"query": "Bạn biết gì về soleil không?", "project_key": "camellia"},
        headers={"accept": "text/event-stream"},
    ) as response:
        raw = "".join(response.iter_text())
    frames = [(block.split("\n", 1)[0][7:], json.loads(block.split("data: ", 1)[1]))
              for block in raw.split("\n\n") if block.startswith("event: ") and "data: " in block]
    routing = next(data for event, data in frames if event == "routing")
    done = next(data for event, data in frames if event == "done")
    redirect = {"project_key": "soleil", "display_name": "The Soleil Đà Nẵng", "short_name": "The Soleil"}
    assert routing["project_redirect"] == done["project_redirect"] == redirect
    assert done["sources"] == done["facts"] == []
    assert "The Soleil Đà Nẵng" in done["answer"]
    assert done["requires_review"] is False
    assert calls == []


@pytest.mark.parametrize("accept", [None, "text/event-stream"])
def test_redirect_persists_with_verified_identity_for_json_and_sse(monkeypatch, request, accept):
    from unittest.mock import AsyncMock

    monkeypatch.setenv("ANON_IDENTITY_SECRET", "redirect-persist-test-secret-0123456789")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    identity_service = AnonymousIdentityService(get_settings().anon_identity_secret)
    token = identity_service.mint_token()
    subject = identity_service.verify_token(token).subject
    append_turn = AsyncMock(return_value=True)
    repository = type("Repository", (), {"append_turn": append_turn})()
    monkeypatch.setattr(
        "api.infrastructure.adapters.postgres_chat_history.repository", repository
    )
    async def reject_quota(**kwargs):
        raise AssertionError("redirect must precede quota")
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: type("Gate", (), {"prepare_turn": reject_quota})(),
    )
    monkeypatch.setattr(
        "api.application.pipelines.workflow.RagQueryWorkflow.run",
        AsyncMock(side_effect=AssertionError("redirect must not retrieve")),
    )
    headers = {"accept": accept} if accept else {}
    response = TestClient(create_app()).post(
        "/query",
        json={
            "query": "Bạn biết gì về soleil không?",
            "project_key": "camellia",
            "session_id": "redirect-persist-session",
            "device_id": "redirect-persist-device",
            "anon_token": token,
        },
        headers=headers,
    )
    if accept:
        frames = [
            (block.split("\n", 1)[0][7:], json.loads(block.split("data: ", 1)[1]))
            for block in response.text.split("\n\n")
            if block.startswith("event: ") and "data: " in block
        ]
        payload = next(data for event, data in frames if event == "done")
    else:
        payload = response.json()

    assert payload["history_persisted"] is True
    append_turn.assert_awaited_once()
    kwargs = append_turn.await_args.kwargs
    assert kwargs["session_id"] == "redirect-persist-session"
    assert kwargs["device_id"] == "redirect-persist-device"
    assert kwargs["project_key"] == "camellia"
    assert kwargs["identity_key"] == subject


@pytest.mark.parametrize("accept", [None, "text/event-stream"])
@pytest.mark.parametrize(
    ("device_id", "anon_token"),
    [(None, "valid-looking-token"), ("redirect-device", None), ("redirect-device", "invalid")],
)
def test_redirect_without_verified_device_identity_does_not_write(
    monkeypatch, device_id, anon_token, accept
):
    from unittest.mock import AsyncMock

    append_turn = AsyncMock(return_value=True)
    repository = type("Repository", (), {"append_turn": append_turn})()
    monkeypatch.setattr(
        "api.infrastructure.adapters.postgres_chat_history.repository", repository
    )
    headers = {"accept": accept} if accept else {}
    response = TestClient(create_app()).post(
        "/query",
        json={
            "query": "Bạn biết gì về soleil không?",
            "project_key": "camellia",
            "session_id": "unowned-redirect-session",
            "device_id": device_id,
            "anon_token": anon_token,
        },
        headers=headers,
    )
    if accept:
        frames = [
            (block.split("\n", 1)[0][7:], json.loads(block.split("data: ", 1)[1]))
            for block in response.text.split("\n\n")
            if block.startswith("event: ") and "data: " in block
        ]
        payload = next(data for event, data in frames if event == "done")
        assert response.headers["content-type"].startswith("text/event-stream")
    else:
        payload = response.json()
    assert payload["history_persisted"] is False
    append_turn.assert_not_awaited()


def test_redirect_turn_preserves_cta_accounting(monkeypatch):
    from api.application.services.conv_state import get_context
    from api.application.services.query_quota_gate import UnmanagedTurnContext

    class Gate:
        async def prepare_turn(self, **kwargs):
            return UnmanagedTurnContext()

    monkeypatch.setattr("api.application.services.query_quota_gate.build_query_quota_gate", Gate)

    from api.application.pipelines.workflow import RagQueryWorkflow

    async def clean_inner_run(self, **kwargs):
        return _clean_inner_result()

    monkeypatch.setattr(RagQueryWorkflow, "run", clean_inner_run)
    client = TestClient(create_app())

    def routing_cta(query, session_id):
        import json

        with client.stream(
            "POST", "/query",
            json={"query": query, "project_key": "camellia", "session_id": session_id},
            headers={"accept": "text/event-stream"},
        ) as response:
            raw = "".join(response.iter_text())
        frames = [(block.split("\n", 1)[0][7:], json.loads(block.split("data: ", 1)[1]))
                  for block in raw.split("\n\n") if block.startswith("event: ") and "data: " in block]
        return next(data["lead_cta_hint"] for event, data in frames if event == "routing")

    normal_session = "cta-parity-normal"
    redirect_session = "cta-parity-redirect"
    assert routing_cta("chính sách bán hàng như thế nào", normal_session) is None
    assert routing_cta("chính sách bán hàng như thế nào", redirect_session) is None
    normal_hint = routing_cta("chính sách bán hàng như thế nào", normal_session)
    redirect_hint = routing_cta("bạn biết gì về soleil không", redirect_session)
    assert normal_hint is not None
    assert redirect_hint == normal_hint
    assert get_context(normal_session).useful_turns >= 1
    assert get_context(redirect_session).useful_turns >= 1


def test_comparison_redirects_at_route(monkeypatch):
    class Gate:
        async def prepare_turn(self, **kwargs):
            raise AssertionError("redirect must precede quota")

    monkeypatch.setattr("api.application.services.query_quota_gate.build_query_quota_gate", Gate)
    body = TestClient(create_app()).post(
        "/query", json={"query": "so sánh camellia với soleil", "project_key": "camellia"}
    ).json()
    assert body["project_redirect"]["project_key"] == "soleil"


def test_normal_flow_unknown_and_current_project(monkeypatch):
    from api.application.services.query_quota_gate import UnmanagedTurnContext
    calls = []

    class Gate:
        async def prepare_turn(self, **kwargs):
            calls.append(kwargs["project_key"])
            return UnmanagedTurnContext()

    class Pipeline:
        async def run(self, *args, **kwargs):
            calls.append(kwargs["project_context"])
            return {"answer": "normal", "sources": [], "facts": [], "images": [], "videos": [], "places": [], "confidence": "HIGH", "requires_review": False, "routing": {"intent": "rag"}, "trace_id": "t", "latency_ms": 1}

    monkeypatch.setattr("api.application.services.query_quota_gate.build_query_quota_gate", Gate)
    monkeypatch.setattr("api.application.pipelines.conv_workflow.RagQueryPipelineConv", Pipeline)
    client = TestClient(create_app())
    for query in ("vinhomes có tốt không", "giá Camellia thế nào"):
        response = client.post("/query", json={"query": query, "project_key": "camellia"})
        assert response.status_code == 200
        assert response.json().get("project_redirect") is None
    assert calls[0] == "camellia" and calls[2] == "camellia"


def test_session_project_is_used_for_route_detection(monkeypatch):
    from api.application.services.conv_state import get_context
    session_id = "route-session-project"
    get_context(session_id).project_key = "camellia"
    class Gate:
        async def prepare_turn(self, **kwargs):
            raise AssertionError("session-bound foreign mention redirects before quota")

    monkeypatch.setattr("api.application.services.query_quota_gate.build_query_quota_gate", Gate)
    body = TestClient(create_app()).post(
        "/query", json={"query": "còn soleil thì sao", "session_id": session_id}
    ).json()
    assert body["project_redirect"]["project_key"] == "soleil"

def test_redirect_then_fresh_soleil_turn_scopes_to_soleil(monkeypatch):
    """A redirect turn must not contaminate the very next turn.

    Turn 1 (camellia pinned, question names soleil) short-circuits before
    quota and never runs retrieval. Turn 2 replays a Soleil question on the
    SAME client with project_key=soleil: the route must resolve Soleil, skip
    the redirect branch, and hand the retrieval pipeline a Soleil-only
    ProjectContext — exactly one pipeline run, no inherited camellia scope.
    """
    from api.application.services.conv_state import get_context
    from api.application.services.query_quota_gate import UnmanagedTurnContext

    session_id = "redirect-then-soleil-session"
    quota_calls: list[str] = []
    runs: list[object] = []

    class Gate:
        async def prepare_turn(self, **kwargs):
            quota_calls.append(kwargs["project_key"])
            return UnmanagedTurnContext()

    class Pipeline:
        async def run(self, *args, **kwargs):
            runs.append(kwargs["project_context"])
            return {
                "answer": "soleil", "sources": [], "facts": [],
                "images": [], "videos": [], "places": [],
                "confidence": "HIGH", "requires_review": False,
                "routing": {"intent": "rag"}, "trace_id": "t", "latency_ms": 1,
            }

    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate", Gate
    )
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv", Pipeline
    )
    client = TestClient(create_app())

    with client.stream(
        "POST", "/query",
        json={"query": "Bạn biết gì về soleil không?", "project_key": "camellia",
              "session_id": session_id},
        headers={"accept": "text/event-stream"},
    ) as response:
        raw = "".join(response.iter_text())
    frames = [(block.split("\n", 1)[0][7:], json.loads(block.split("data: ", 1)[1]))
              for block in raw.split("\n\n") if block.startswith("event: ") and "data: " in block]
    redirect = {
        "project_key": "soleil",
        "display_name": "The Soleil Đà Nẵng",
        "short_name": "The Soleil",
    }
    routing = next(data for event, data in frames if event == "routing")
    done = next(data for event, data in frames if event == "done")
    assert routing["project_redirect"] == done["project_redirect"] == redirect
    assert done["sources"] == done["facts"] == []
    assert "The Soleil Đà Nẵng" in done["answer"]
    # Turn 1 stopped before quota reservation and before retrieval.
    assert quota_calls == []
    assert runs == []
    # The redirect turn DID write conversation state — bound to the pinned
    # camellia — so turn 2's soleil scope is a real rebind, not a no-op.
    assert get_context(session_id).project_key == "camellia"

    response = client.post(
        "/query", json={"query": "tiện ích soleil", "project_key": "soleil",
                        "session_id": session_id}
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("project_redirect") is None
    # Turn 2 is a normal Soleil turn: quota reserved for soleil only, and
    # retrieval ran exactly once with a Soleil-scoped ProjectContext.
    assert quota_calls == ["soleil"]
    assert len(runs) == 1
    assert runs[0].project_key == "soleil"

# --- GET /api/projects contract -------------------------------------------------


def _registry_record(key: str, name: str, *, hot: bool) -> ProjectRegistryRecord:
    return ProjectRegistryRecord(
        project_key=key,
        ten_thuong_mai=name,
        ten_phap_ly=None,
        vi_tri=None,
        hotline=None,
        location=f"location {key}",
        geo_center_lat=16.1,
        geo_center_lng=108.25,
        is_hot=hot,
        status="active",
    )


def test_catalogue_ordering_pins_default_first_then_others() -> None:
    rows = project_catalogue_from_records(
        [
            _registry_record("zeta", "Zeta Heights", hot=True),
            _registry_record("soleil", KNOWN_PROJECTS[1][1], hot=False),
            _registry_record("camellia", "The Camellia Son Tra - Da Nang", hot=True),
        ]
    )
    assert [r["project_key"] for r in rows] == ["camellia", "soleil", "zeta"]
    assert rows[0]["display_name"] == "The Camellia Son Tra - Da Nang"
    # Short human label: marketing qualifier stripped for the picker/button.
    assert rows[1]["display_name"] == "The Soleil Đà Nẵng"
    # City token additionally stripped for compact UI copy (both spellings).
    assert rows[1]["short_name"] == "The Soleil"
    assert rows[0]["short_name"] == "The Camellia Son Tra"
    assert rows[2]["short_name"] == "Zeta Heights"
    # Legacy fields stay for existing consumers.
    assert rows[1]["name"].startswith("The Soleil Đà Nẵng (")
    assert rows[1]["is_hot"] is False
    assert rows[1]["lat"] == 16.1


def test_projects_endpoint_serves_contract_shape_and_order(monkeypatch) -> None:
    # Already contract-ordered by fetch_projects (default first): the endpoint
    # is a pure pass-through and must preserve both order and row shape.
    canned = [
        {
            "project_key": "camellia",
            "name": "The Camellia Son Tra - Da Nang",
            "display_name": "The Camellia Son Tra - Da Nang",
            "short_name": "The Camellia Son Tra",
            "location": "Giao lộ Lê Văn Lương - Lê Đức Thọ",
            "lat": 16.1052,
            "lng": 108.2558,
            "is_hot": True,
        },
        {
            "project_key": "soleil",
            "name": KNOWN_PROJECTS[1][1],
            "display_name": "The Soleil Đà Nẵng",
            "short_name": "The Soleil",
            "location": "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp",
            "lat": 16.0710756,
            "lng": 108.2436243,
            "is_hot": False,
        },
    ]
    monkeypatch.setattr("api.application.services.project_config.fetch_projects", lambda: canned)
    client = TestClient(create_app())
    response = client.get("/api/projects")
    assert response.status_code == 200
    body = response.json()
    assert [p["project_key"] for p in body["projects"]] == ["camellia", "soleil"]
    for row in body["projects"]:
        assert set(row) >= {"project_key", "display_name", "short_name"}
        assert isinstance(row["display_name"], str) and row["display_name"]
        assert isinstance(row["short_name"], str) and row["short_name"]
