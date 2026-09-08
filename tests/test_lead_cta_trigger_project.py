"""Regression tests — lead CTA trigger must behave identically per project (story 5.7).

User-reported bug: on The Soleil the bot NEVER asked lead qualifying questions,
while the Camellia flow worked. The §6.5 decision chain itself has NO project
condition (maybe_lead_cta_hint reads only useful_turns / phone_given / CTA
spacing / previous review status / cta_shown_count), so the observed difference
can only come from its INPUTS: useful_turns and last_answer_reviewed are earned
exclusively by clean (requires_review=False) answers. While the Soleil corpus
sat on the reserved '_legacy' documents.project_key, every Soleil answer came
back LOW/ungrounded, so the hint starved forever (see
db/migrations/2026-08-24-fix-documents-project-key.sql and
tests/test_project_isolation_regression.py).

Pinned contracts (through RagRgreConvWorkflow — the class actually wired
behind POST /query):

1. First turn of ANY project stays silent (gate (a): useful_turns == 0 until
   an answer lands).
2. Project parity: with grounding working (clean answer on turn 1), the
   turn-2 routing event carries lead_cta_hint for soleil EXACTLY like it does
   for camellia — no project may gate the funnel.
3. Starvation mode (documented spec behavior): a session whose answers are
   always requires_review=True never emits the hint and never accrues
   useful turns — the exact mechanism that produced the report while Soleil
   retrieval was mis-scoped.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.application.pipelines.conv_workflow import (
    SSE_EVENT_ROUTING,
    RagRgreConvWorkflow,
)
from api.application.services.conv_state import (
    CTA_VARIANTS,
    get_context,
)


class _ScriptedInner:
    """Fake inner RagQueryWorkflow popping one canned result per turn.

    Keeps the conv integration tests off the real RAG pipeline / DB / LLM;
    the sequence models what guard_output would produce for grounded vs
    ungrounded answers without running it.
    """

    def __init__(self, results: list[dict[str, Any]]):
        self._results = list(results)

    def run(self, **kwargs):
        return self._handler()

    async def _handler(self):
        return self._results.pop(0)


def _routing_frames(events: list[tuple[str, dict]]) -> list[dict]:
    return [data for event, data in events if event == SSE_EVENT_ROUTING]


@pytest.fixture(autouse=True)
def _hermetic_project_identity(monkeypatch):
    """Serve registry identity from static dicts so tests never touch PG.

    conv_directive re-imports fetch_project_identity at call time and
    conv_workflow holds a direct brand_token reference, so both seam modules
    are patched; the identity values mirror db/seed/project_config.sql.
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


@pytest.mark.asyncio
async def test_first_turn_stays_silent_before_any_useful_answer():
    """Gate (a): a fresh session has useful_turns == 0, so turn 1 never asks."""
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    wf._inner = _ScriptedInner([{"answer": "Giá tham khảo ...", "requires_review": False}])
    await wf.run(
        query="bảng giá The Soleil bao nhiêu",
        session_id="cta_proj_first_soleil",
        history=[],
        project_key="soleil",
    )
    routing = _routing_frames(events)
    assert len(routing) == 1
    assert routing[0]["lead_cta_hint"] is None
    # The clean answer landed afterwards: the NEXT turn becomes askable.
    assert get_context("cta_proj_first_soleil").useful_turns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("project_key", ["soleil", "camellia"])
async def test_second_turn_emits_lead_cta_hint_for_every_project(project_key: str):
    """Project parity: one clean answer makes turn 2 ask for the phone.

    This is the contract the user-visible bug violated: with Soleil grounding
    restored (corpus tagged off '_legacy'), Soleil must qualify exactly like
    Camellia — the funnel may never depend on which project is active.
    """
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    wf._inner = _ScriptedInner(
        [
            {"answer": "Chính sách bán hàng ...", "requires_review": False},
            {"answer": "Tiến độ thanh toán ...", "requires_review": False},
        ]
    )
    session_id = f"cta_proj_parity_{project_key}"
    await wf.run(
        query="chính sách bán hàng như thế nào",
        session_id=session_id,
        history=[],
        project_key=project_key,
    )
    await wf.run(
        query="tiến độ thanh toán ra sao",
        session_id=session_id,
        history=[{"role": "user", "content": "chính sách bán hàng như thế nào"}],
        project_key=project_key,
    )
    routing = _routing_frames(events)
    assert len(routing) == 2
    assert routing[0]["lead_cta_hint"] is None  # turn 1: nothing useful yet
    hint = routing[1]["lead_cta_hint"]
    assert hint in CTA_VARIANTS
    # The session remembers which project it serves (story 10.1) ...
    assert get_context(session_id).project_key == project_key


@pytest.mark.asyncio
async def test_all_low_answers_starve_the_cta_forever():
    """Documented starvation mode behind the reported bug.

    While Soleil retrieval returned nothing project-scoped, every answer was
    requires_review=True: gate (a) never unlocks (note_useful_turn requires a
    clean answer) and gate (d) blocks every subsequent turn. The hint must
    stay None — pushing the funnel after untrusted answers is forbidden —
    which is why fixing the DATA scope was the actual remediation.
    """
    events: list[tuple[str, dict]] = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    wf._inner = _ScriptedInner(
        [
            {"answer": "Xin lỗi tôi chưa có thông tin ...", "requires_review": True},
            {"answer": "Tôi chưa được cập nhật ...", "requires_review": True},
            {"answer": "Vẫn chưa tìm thấy ...", "requires_review": True},
        ]
    )
    session_id = "cta_proj_starve_soleil"
    for i, q in enumerate(
        (
            "bảng giá The Soleil",
            "chính sách thanh toán",
            "chuẩn bàn giao thế nào",
        )
    ):
        await wf.run(
            query=q,
            session_id=session_id,
            history=[{"role": "user", "content": f"q{i}"}] if i else [],
            project_key="soleil",
        )
    routing = _routing_frames(events)
    assert len(routing) == 3
    assert all(frame["lead_cta_hint"] is None for frame in routing)
    ctx = get_context(session_id)
    assert ctx.useful_turns == 0
    assert ctx.cta_shown_count == 0
