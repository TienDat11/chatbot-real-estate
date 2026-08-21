"""Story 5.7: customer lead submission over HTTP (POST /api/lead)."""

from __future__ import annotations

from fastapi.testclient import TestClient

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
        json={"session_id": "session-1", "phone": "0905 123 456", "consent": True},
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
        json={"phone": "+84 905 123 456", "consent": True},
    )
    assert response.status_code == 201
    assert repo.leads[1].phone == "+84905123456"


def test_submit_lead_rejects_missing_consent_with_400() -> None:
    client, _ = make_client()
    response = client.post("/api/lead", json={"phone": "0905123456", "consent": False})
    assert response.status_code == 400
    assert response.json()["detail"] == "Consent is required"


def test_submit_lead_rejects_invalid_phone_with_422() -> None:
    client, repo = make_client()
    response = client.post("/api/lead", json={"phone": "0301234567", "consent": True})
    assert response.status_code == 422
    assert repo.leads == {}


def test_submit_lead_rejects_invalid_phone_that_is_otherwise_vietnamese() -> None:
    # 09x is a valid prefix, but 9 digits after it is not a valid VN number.
    client, _ = make_client()
    response = client.post("/api/lead", json={"phone": "090512345", "consent": True})
    assert response.status_code == 422


def test_submit_lead_strips_name_and_note_whitespace() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={
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
