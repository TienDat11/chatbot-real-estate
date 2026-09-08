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

from typing import Any

import pytest
from fastapi.testclient import TestClient

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
    """Serve registry identity from static dicts so tests never touch PG.

    Same seams as tests/test_lead_cta_trigger_project.py: conv_directive
    re-imports fetch_project_identity at call time and conv_workflow holds a
    direct brand_token reference.
    """
    identities = {
        "camellia": {
            "ten_thuong_mai": "The Camellia Son Tra - Da Nang",
            "ten_phap_ly": "Trung tâm Thương mại, văn phòng cho thuê và nhà ở cao tầng",
            "vi_tri": "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Sơn Trà, Đà Nẵng",
            "hotline": "0345 747 138",
        },
        "soleil": {
            "ten_thuong_mai": "The Soleil Đà Nẵng",
            "ten_phap_ly": "Tổ hợp Ánh Dương - Soleil",
            "vi_tri": "Đà Nẵng",
            "hotline": "0905 000 000",
        },
    }

    def fake_identity(project_key=None):
        return dict(identities.get(project_key or "camellia", identities["camellia"]))

    def fake_brand_token(project_key=None):
        return (project_key or "camellia").lower()

    monkeypatch.setattr(
        "api.application.services.project_config.fetch_project_identity", fake_identity
    )
    monkeypatch.setattr("api.application.pipelines.conv_workflow.brand_token", fake_brand_token)


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


# --- conv workflow: guard before retrieval -------------------------------------


@pytest.mark.asyncio
async def test_soleil_question_in_camellia_redirects_without_retrieval():
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    inner = _RecordingInner(_clean_inner_result())
    wf._inner = inner
    result = await wf.run(
        query="Bạn biết gì về soleil không?",
        session_id="guard_soleil_in_camellia",
        history=[],
        project_key="camellia",
        known_projects=KNOWN_PROJECTS,
    )
    routing = _routing_frames(events)
    assert len(routing) == 1
    assert routing[0]["project_redirect"] == {
        "project_key": "soleil",
        "display_name": "The Soleil Đà Nẵng",
        "short_name": "The Soleil",
    }
    # Retrieval never ran: no inner invocation, no citations by construction.
    assert inner.calls == []
    assert result["sources"] == []
    assert result["facts"] == []
    assert "The Soleil Đà Nẵng" in result["answer"]
    # Switch instruction uses the compact short name.
    assert "chuyển sang dự án The Soleil" in result["answer"]
    assert result["requires_review"] is False
    assert result["project_redirect"]["project_key"] == "soleil"


@pytest.mark.asyncio
async def test_guard_turn_feeds_cta_accounting_like_a_clean_answer():
    """Redirect turn counts as useful, so turn 2 keeps asking for the phone."""
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    inner = _RecordingInner(_clean_inner_result())
    wf._inner = inner
    session_id = "guard_cta_parity"
    await wf.run(
        query="bạn biết gì về soleil không",
        session_id=session_id,
        history=[],
        project_key="camellia",
        known_projects=KNOWN_PROJECTS,
    )
    assert get_context(session_id).useful_turns == 1
    await wf.run(
        query="chính sách bán hàng như thế nào",
        session_id=session_id,
        history=[{"role": "user", "content": "bạn biết gì về soleil không"}],
        project_key="camellia",
        known_projects=KNOWN_PROJECTS,
    )
    routing = _routing_frames(events)
    assert len(routing) == 2
    assert "project_redirect" not in routing[1]
    assert routing[1]["lead_cta_hint"] in CTA_VARIANTS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "bảng giá The Camellia bao nhiêu",  # current project mentioned
        "chuẩn bàn giao thế nào ạ",  # no project mentioned
        "vinhomes có tốt không vậy",  # unknown third-party name
    ],
)
async def test_normal_flow_untouched_when_guard_does_not_fire(query: str):
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    inner = _RecordingInner(_clean_inner_result())
    wf._inner = inner
    result = await wf.run(
        query=query,
        session_id=f"guard_normal_{abs(hash(query))}",
        history=[],
        project_key="camellia",
        known_projects=KNOWN_PROJECTS,
    )
    assert len(inner.calls) == 1  # the inner workflow ran normally
    assert all("project_redirect" not in frame for frame in _routing_frames(events))
    assert result["answer"] == _clean_inner_result()["answer"]
    assert "project_redirect" not in result


@pytest.mark.asyncio
async def test_comparison_question_redirects_inside_workflow():
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    inner = _RecordingInner(_clean_inner_result())
    wf._inner = inner
    result = await wf.run(
        query="so sánh camellia với soleil được không",
        session_id="guard_comparison",
        history=[],
        project_key="camellia",
        known_projects=KNOWN_PROJECTS,
    )
    assert inner.calls == []
    assert result["project_redirect"]["project_key"] == "soleil"


@pytest.mark.asyncio
async def test_session_bound_project_gates_detection_when_arg_missing():
    """A follow-up without project_key still uses the session-bound project."""
    session_id = "guard_session_bound"
    get_context(session_id).project_key = "camellia"
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    inner = _RecordingInner(_clean_inner_result())
    wf._inner = inner
    result = await wf.run(
        query="còn soleil thì sao",
        session_id=session_id,
        history=[],
        project_key=None,  # FE omitted it; the session remembers camellia
        known_projects=KNOWN_PROJECTS,
    )
    assert inner.calls == []
    assert result["project_redirect"]["project_key"] == "soleil"


@pytest.mark.asyncio
async def test_direct_caller_without_known_projects_degrades_to_seed_mirror():
    """No known_projects kwarg -> static seed mirror keeps the guard alive."""
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    inner = _RecordingInner(_clean_inner_result())
    wf._inner = inner
    result = await wf.run(
        query="bạn biết gì về the soleil không",
        session_id="guard_seed_mirror",
        history=[],
        project_key="camellia",
    )
    assert inner.calls == []
    assert result["project_redirect"]["project_key"] == "soleil"


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
