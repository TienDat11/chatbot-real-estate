"""Story 5.7: customer lead submission over HTTP (POST /api/lead)."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from api.application.services.conv_state import get_context, maybe_lead_cta_hint
from api.application.services.lead_service import DuplicateLeadError
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
        json={"project_key": "camellia", "session_id": "session-1", "phone": "0905 123 456", "consent": True},
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
    response = client.post("/api/lead", json={"project_key": "camellia", "phone": "0905123456", "consent": False})
    assert response.status_code == 400
    assert response.json()["detail"] == "Consent is required"


def test_submit_lead_rejects_invalid_phone_with_422() -> None:
    client, repo = make_client()
    response = client.post("/api/lead", json={"project_key": "camellia", "phone": "0301234567", "consent": True})
    assert response.status_code == 422
    assert repo.leads == {}


def test_submit_lead_rejects_invalid_phone_that_is_otherwise_vietnamese() -> None:
    # 09x is a valid prefix, but 9 digits after it is not a valid VN number.
    client, _ = make_client()
    response = client.post("/api/lead", json={"project_key": "camellia", "phone": "090512345", "consent": True})
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
        json={"project_key": "camellia", "session_id": session_id, "phone": "0905123456", "consent": True},
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


class DeduplicatingFakeRepository(FakeLeadRepository):
    """Fake mirroring the Postgres adapter dedup contract (QA D3): a second
    create with the same identity raises DuplicateLeadError instead of
    inserting another row."""

    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    async def create_lead(self, **kwargs):
        self.create_calls += 1
        if self.create_calls > 1:
            first = next(iter(self.leads.values()))
            raise DuplicateLeadError(lead_id=first.id, created_at=first.created_at)
        return await super().create_lead(**kwargs)


def test_submit_lead_duplicate_returns_409_with_structured_body() -> None:
    # QA D3: the FE maps any 409 to the "duplicate" UX state (submitLead);
    # the detail must be a structured object so the code is machine-readable.
    repo = DeduplicatingFakeRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    client = TestClient(app)
    payload = {"project_key": "camellia", "phone": "0905123456", "consent": True}

    first = client.post("/api/lead", json=payload)
    assert first.status_code == 201
    assert repo.create_calls == 1

    duplicate = client.post("/api/lead", json=payload)
    assert duplicate.status_code == 409
    detail = duplicate.json()["detail"]
    assert detail["code"] == "duplicate_lead"
    assert detail["lead_id"] == first.json()["lead_id"]
    assert detail["created_at"] == repo.leads[1].created_at.isoformat()
    assert isinstance(detail["message"], str) and detail["message"]
    # The duplicate never reaches routing: one insert, one assignment log.
    assert repo.create_calls == 2
    assert len(repo.logs) == 1
