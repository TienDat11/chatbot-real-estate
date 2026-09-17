"""GA-BE-08 retrieval intent, raw persistence, and resolution regressions."""

from unittest.mock import AsyncMock

import pytest

from api.application.services import quota_service
from api.application.services.global_assistant_service import (
    GlobalAssistantInput,
    GlobalAssistantService,
)
from api.application.services.global_project_resolution import (
    ProjectResolutionSource,
    ProjectResolutionStatus,
)
from tests.test_global_assistant_service import (
    FakeHistory,
    FakeMessage,
    FakeSession,
    _catalogue,
    _pipeline_factory,
)


@pytest.fixture
def quota_consume(monkeypatch):
    consume = AsyncMock()
    monkeypatch.setattr(quota_service, "consume_turn", consume)
    return consume


def _pending_messages():
    return [
        FakeMessage("user", "Giá căn hai phòng ngủ bao nhiêu?"),
        FakeMessage(
            "assistant",
            "Bạn muốn hỏi dự án nào?",
            meta={"ga_outcome": {"resolution_status": "unresolved"}},
        ),
    ]


@pytest.mark.asyncio
async def test_selected_clarification_recovers_intent_but_persists_raw_query():
    captured = {}
    history = FakeHistory(messages=_pending_messages())
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="Chọn dự án thứ nhất", session_id="s", identity_key="i",
        selected_project_key="camellia",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert outcome.resolution_source == ProjectResolutionSource.USER_SELECTION
    assert outcome.project_key == "camellia"
    assert captured["query"] == "Giá căn hai phòng ngủ bao nhiêu?\nChọn dự án thứ nhất"
    assert captured["project_key"] == "camellia"
    assert history.appended[0]["user_content"] == request.query
    assert history.appended[0]["resolved_project_key"] == "camellia"


@pytest.mark.asyncio
async def test_resolved_latest_turn_does_not_fabricate_pending_intent():
    captured = {}
    history = FakeHistory(messages=[
        FakeMessage("user", "Câu hỏi đã trả lời", project_key="soleil"),
        FakeMessage("assistant", "Đã trả lời", project_key="soleil",
                    meta={"ga_outcome": {"resolution_status": "resolved"}}),
    ])
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="Chọn dự án thứ nhất", session_id="s", identity_key="i",
        selected_project_key="camellia",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert prepared.effective_query == request.query
    assert outcome.resolution_source == ProjectResolutionSource.USER_SELECTION
    assert captured["query"] == request.query
    assert history.appended[0]["user_content"] == request.query


@pytest.mark.asyncio
async def test_second_project_selection_does_not_reuse_consumed_intent():
    captured = {}
    history = FakeHistory(session=FakeSession(), messages=_pending_messages())
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    await service.run(GlobalAssistantInput(
        query="Chọn dự án thứ nhất", session_id="s", identity_key="i",
        selected_project_key="camellia",
    ))
    assert captured["query"] == "Giá căn hai phòng ngủ bao nhiêu?\nChọn dự án thứ nhất"
    # Feed actual persisted arguments back through the existing history seam.
    saved = history.appended[0]
    history.messages.extend([
        FakeMessage("user", saved["user_content"], project_key=saved["resolved_project_key"]),
        FakeMessage("assistant", saved["assistant_content"],
                    project_key=saved["resolved_project_key"], meta=saved["assistant_meta"]),
    ])
    history.session.active_project_key = saved["resolved_project_key"]
    request = GlobalAssistantInput(
        query="Chọn dự án thứ hai", session_id="s", identity_key="i",
        selected_project_key="soleil",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert prepared.effective_query == request.query
    assert outcome.resolution_source == ProjectResolutionSource.USER_SELECTION
    assert outcome.project_key == "soleil"
    assert captured["query"] == request.query
    assert captured["project_key"] == "soleil"
    assert history.appended[1]["user_content"] == request.query
    assert history.appended[1]["resolved_project_key"] == "soleil"


@pytest.mark.asyncio
async def test_inactive_selection_stays_unresolved_without_rag_or_quota(quota_consume):
    captured = {}
    history = FakeHistory(messages=_pending_messages())
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="Chọn dự án đã đóng", session_id="s", identity_key="i",
        selected_project_key="inactive-project",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert prepared.resolution.status is ProjectResolutionStatus.UNRESOLVED
    assert prepared.resolution.candidates == ()
    assert outcome.project_key is None
    assert outcome.rag_ran is False
    assert captured == {}
    quota_consume.assert_not_awaited()
    assert history.appended[0]["resolved_project_key"] is None
    assert history.appended[0]["user_content"] == request.query


@pytest.mark.asyncio
async def test_explicit_switch_wins_over_session_and_selection():
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key="camellia"),
                          messages=_pending_messages())
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="Soleil có hồ bơi không?", session_id="s", identity_key="i",
        selected_project_key="camellia", route_project_key="camellia",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert outcome.resolution_source == ProjectResolutionSource.EXPLICIT_QUERY
    assert outcome.project_key == "soleil"
    assert captured["project_key"] == "soleil"
    assert captured["query"] == request.query
    assert history.appended[0]["resolved_project_key"] == "soleil"
    assert history.appended[0]["user_content"] == request.query


@pytest.mark.asyncio
async def test_ambiguous_query_keeps_both_candidates_without_rag_or_quota(quota_consume):
    captured = {}
    history = FakeHistory(session=FakeSession(active_project_key="camellia"))
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="So sánh Camellia và Soleil", session_id="s", identity_key="i",
        selected_project_key="camellia", route_project_key="camellia",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert prepared.resolution.status is ProjectResolutionStatus.AMBIGUOUS
    assert prepared.resolution.candidates == ("camellia", "soleil")
    assert outcome.candidates == ("camellia", "soleil")
    assert outcome.project_key is None
    assert outcome.rag_ran is False
    assert captured == {}
    quota_consume.assert_not_awaited()
    assert history.appended[0]["resolved_project_key"] is None
    assert history.appended[0]["assistant_meta"]["ga_outcome"]["project_key"] is None
    assert history.appended[0]["user_content"] == request.query


@pytest.mark.asyncio
async def test_source_event_replay_ignores_newer_pending_intent():
    captured = {}
    history = FakeHistory()
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="Chọn dự án thứ nhất", session_id="s", identity_key="i",
        selected_project_key="camellia", source_event_key="event-1",
    )
    original = await service.run(request)
    saved = history.appended[0]
    history.messages = [
        FakeMessage("user", saved["user_content"], project_key=saved["resolved_project_key"]),
        FakeMessage("assistant", saved["assistant_content"],
                    project_key=saved["resolved_project_key"], meta=saved["assistant_meta"]),
        *_pending_messages(),
    ]
    captured.clear()
    history.appended.clear()

    replay = await service.run(request)

    assert replay.replayed is True
    assert replay.answer == original.answer
    assert replay.project_key == "camellia"
    assert replay.rag_ran is False
    assert captured == {}
    assert history.appended == []


@pytest.mark.asyncio
async def test_unresolved_never_defaults_to_catalogue_project(quota_consume):
    captured = {}
    history = FakeHistory()
    service = GlobalAssistantService(
        history=history, pipeline_factory=_pipeline_factory(captured), catalogue_loader=_catalogue
    )
    request = GlobalAssistantInput(
        query="Dự án nào có hồ bơi?", session_id="s", identity_key="i",
    )

    prepared = await service.prepare_turn(request)
    outcome = await service.execute_prepared_turn(prepared)

    assert prepared.resolution.status is ProjectResolutionStatus.UNRESOLVED
    assert prepared.resolution.candidates == ()
    assert prepared.resolution.project_key is None
    assert outcome.project_key is None
    assert outcome.rag_ran is False
    assert captured == {}
    quota_consume.assert_not_awaited()
    # Outcome may offer the catalogue as picker options; resolver never guesses.
    assert history.appended[0]["resolved_project_key"] is None
    assert history.appended[0]["user_content"] == request.query
