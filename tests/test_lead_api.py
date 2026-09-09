"""Story 5.7: customer lead submission over HTTP (POST /api/lead)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.application.services.conv_state import get_context, maybe_lead_cta_hint
from api.infrastructure.ports.leads import get_lead_repository
from api.interfaces.api.main import create_app
from tests.test_sales_api import FakeLeadRepository


def make_client() -> tuple[TestClient, FakeLeadRepository]:
    """Build an app whose lead persistence is backed by the in-memory fake."""
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    return TestClient(app), repo


def test_submit_lead_returns_201_with_lead_id_and_call_window() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "session_id": "session-1",
            "phone": "0905 123 456",
            "consent": True,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["lead_id"] == 1
    assert body["will_call_within_minutes"] == 5
    # The stored phone is normalized (separators stripped) before persistence.
    assert repo.leads[1].phone == "0905123456"
    # Every accepted lead is assigned to a sales agent (Story 6.4 routing).
    assert repo.leads[1].status == "assigned"
    assert repo.leads[1].assigned_sales_id == 1
    assert repo.logs[-1].action == "assign"


def test_submit_lead_duplicate_returns_429_without_500() -> None:
    client, _ = make_client()
    payload = {
        "project_key": "camellia",
        "phone": "0905123456",
        "consent": True,
    }

    first = client.post("/api/lead", json=payload)
    duplicate = client.post("/api/lead", json=payload)

    assert first.status_code == 201
    assert duplicate.status_code == 429
    assert duplicate.json()["detail"] == "A lead with this phone number was submitted recently"


def test_submit_lead_normalizes_plus84_prefix() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={"project_key": "camellia", "phone": "+84 905 123 456", "consent": True},
    )
    assert response.status_code == 201
    assert repo.leads[1].phone == "+84905123456"


def test_submit_lead_rejects_missing_consent_with_400() -> None:
    client, _ = make_client()
    response = client.post(
        "/api/lead", json={"project_key": "camellia", "phone": "0905123456", "consent": False}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Consent is required"


def test_submit_lead_rejects_invalid_phone_with_422() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead", json={"project_key": "camellia", "phone": "0301234567", "consent": True}
    )
    assert response.status_code == 422
    assert repo.leads == {}


def test_submit_lead_rejects_invalid_phone_that_is_otherwise_vietnamese() -> None:
    # 09x is a valid prefix, but 9 digits after it is not a valid VN number.
    client, _ = make_client()
    response = client.post(
        "/api/lead", json={"project_key": "camellia", "phone": "090512345", "consent": True}
    )
    assert response.status_code == 422


def test_submit_lead_strips_name_and_note_whitespace() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "session_id": "s1",
            "name": "  Anh Test  ",
            "phone": "0905123456",
            "consent": True,
            "note": "  Quan tâm căn 2PN  ",
        },
    )
    assert response.status_code == 201
    assert repo.leads[1].name == "Anh Test"
    assert repo.leads[1].note == "Quan tâm căn 2PN"


def test_submit_lead_marks_session_handoff_done() -> None:
    # §6.7: a successful lead submit must flip the session to phone_given +
    # handoff_done, otherwise gate (b) of maybe_lead_cta_hint never blocks and
    # the customer is asked for a phone again after already providing it.
    session_id = "session-handoff"
    ctx = get_context(session_id)
    ctx.useful_turns = 1  # make (a) pass so only (b) can suppress the hint
    client, _ = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "session_id": session_id,
            "phone": "0905123456",
            "consent": True,
        },
    )
    assert response.status_code == 201
    assert ctx.slots.get("phone_given") is True
    assert ctx.state == "handoff_done"
    assert maybe_lead_cta_hint(ctx) is None


def test_submit_lead_without_session_skips_state_marking() -> None:
    # Anonymous submits must not mint a throwaway conv_state entry.
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={"project_key": "camellia", "phone": "0905123456", "consent": True},
    )
    assert response.status_code == 201
    assert repo.leads[1].session_id is None


# --- durable session handoff (CRM lead conversation must not 404) -----------
#
# QA regression (leads 177/179): a lead submitted from an existing customer
# chat session must persistently link that session to the lead so
# GET /api/crm/leads/{id}/conversation resolves the transcript for the
# assigned sales. The FE ships the anon token in the BODY (quota bonus), not
# as an X-Anon-Token header, so the handoff must accept the device claim and
# the body token instead of demanding the header.

from datetime import datetime, timezone  # noqa: E402

from api.application.services.chat_history_service import ChatSessionSummary  # noqa: E402
from api.infrastructure.adapters import postgres_chat_history  # noqa: E402


def _handoff_session(device_id: str | None, identity_key: str | None) -> ChatSessionSummary:
    return ChatSessionSummary(
        session_id="session-177",
        device_id=device_id,
        project_key="camellia",
        identity_key=identity_key,
        title="Cho xem mat bang",
        message_count=4,
        handed_off=False,
        last_active_at=datetime.now(timezone.utc),
    )


def test_submit_lead_links_session_by_device_claim_without_anon_token(monkeypatch) -> None:
    """Device-claim-only submit (the FE contract) must mark the session handed off."""
    handed_off: list[tuple[str, int]] = []

    async def fake_get_session_for_handoff(**kwargs):
        assert kwargs["session_id"] == "session-177"
        assert kwargs["project_key"] == "camellia"
        assert kwargs["device_id"] == "device-177"
        assert kwargs["identity_key"] is None
        return _handoff_session("device-177", None)

    async def fake_mark_handed_off(*, session_id: str, lead_id: int) -> None:
        handed_off.append((session_id, lead_id))

    monkeypatch.setattr(
        postgres_chat_history.repository, "get_session_for_handoff", fake_get_session_for_handoff
    )
    monkeypatch.setattr(postgres_chat_history.repository, "mark_handed_off", fake_mark_handed_off)

    client, _ = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "session_id": "session-177",
            "device_id": "device-177",
            "phone": "0905123456",
            "consent": True,
        },
    )
    assert response.status_code == 201
    assert handed_off == [("session-177", 1)]


def test_submit_lead_handoff_skipped_for_unclaimed_session(monkeypatch) -> None:
    """No verifiable claim matches -> fail-closed: the session stays unlinked."""

    async def fake_get_session_for_handoff(**kwargs):
        assert kwargs["device_id"] == "device-other"
        return None

    async def fail_mark_handed_off(*, session_id: str, lead_id: int) -> None:
        raise AssertionError("handoff must not fire without an ownership claim")

    monkeypatch.setattr(
        postgres_chat_history.repository, "get_session_for_handoff", fake_get_session_for_handoff
    )
    monkeypatch.setattr(postgres_chat_history.repository, "mark_handed_off", fail_mark_handed_off)

    client, _ = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "session_id": "session-179",
            "device_id": "device-other",
            "phone": "0905123456",
            "consent": True,
        },
    )
    assert response.status_code == 201


def test_submit_lead_handoff_blocked_by_conflicting_device_claims(monkeypatch) -> None:
    """Header vs body device disagreement stays rejected even if the row matches."""

    async def fake_get_session_for_handoff(**kwargs):
        return _handoff_session("device-1", None)

    async def fail_mark_handed_off(*, session_id: str, lead_id: int) -> None:
        raise AssertionError("conflicting device claims must never link a session")

    monkeypatch.setattr(
        postgres_chat_history.repository, "get_session_for_handoff", fake_get_session_for_handoff
    )
    monkeypatch.setattr(postgres_chat_history.repository, "mark_handed_off", fail_mark_handed_off)

    client, _ = make_client()
    response = client.post(
        "/api/lead",
        headers={"X-Device-Id": "device-2"},
        json={
            "project_key": "camellia",
            "session_id": "session-177",
            "device_id": "device-1",
            "phone": "0905123456",
            "consent": True,
        },
    )
    assert response.status_code == 201
