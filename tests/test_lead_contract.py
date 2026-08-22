"""Story 10.1-BE: lead contract requires project_key (G1) + device_id capture.

POST /api/lead now rejects any submission without a project_key (422), validates
its shape against the project-key regex, and persists device_id alongside the
phone. Every accepted lead must carry a project_key that came from the request —
never guessed from the session (G1).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.application.services.project_scope import (
    ProjectScopeError,
    resolve_project_key,
    validate_project_key,
)
from api.infrastructure.ports.leads import get_lead_repository
from api.interfaces.api.main import create_app
from tests.test_sales_api import FakeLeadRepository


def make_client() -> tuple[TestClient, FakeLeadRepository]:
    """Build an app whose lead persistence is backed by the in-memory fake."""
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    return TestClient(app), repo


# --- project_key validation (regex ^[a-z0-9_]{2,40}$ + reserved keys) -------

def test_validate_project_key_accepts_legal_slugs() -> None:
    for key in ("camellia", "soleil", "my_project_2", "a1"):
        validate_project_key(key)  # must not raise


def test_validate_project_key_rejects_bad_shape() -> None:
    for key in ("", "Camelia", "camellia!", "c", "x" * 41, "camellia-son-tra"):
        try:
            validate_project_key(key)
        except ProjectScopeError:
            continue
        raise AssertionError(f"expected ProjectScopeError for {key!r}")


def test_validate_project_key_rejects_reserved_keys() -> None:
    for key in ("_legacy", "_training"):
        try:
            validate_project_key(key)
        except ProjectScopeError:
            continue
        raise AssertionError(f"expected ProjectScopeError for reserved {key!r}")


# --- default rule: >1 active -> 422-style error; exactly 1 -> that project -----

@pytest.mark.asyncio
async def test_resolve_project_key_defaults_when_single_active() -> None:
    active = [type("P", (), {"project_key": "camellia", "ten_thuong_mai": "C"})()]
    assert await resolve_project_key(None, active_projects=active) == "camellia"


@pytest.mark.asyncio
async def test_resolve_project_key_requires_choice_when_many_active() -> None:
    active = [
        type("P", (), {"project_key": "camellia", "ten_thuong_mai": "C"})(),
        type("P", (), {"project_key": "soleil", "ten_thuong_mai": "S"})(),
    ]
    try:
        await resolve_project_key(None, active_projects=active)
    except ProjectScopeError:
        return
    raise AssertionError("expected ProjectScopeError with multiple active projects")


@pytest.mark.asyncio
async def test_resolve_project_key_rejects_inactive_requested() -> None:
    active = [type("P", (), {"project_key": "camellia", "ten_thuong_mai": "C"})()]
    try:
        await resolve_project_key("soleil", active_projects=active)
    except ProjectScopeError:
        return
    raise AssertionError("expected ProjectScopeError for inactive requested key")


# --- HTTP contract -----------------------------------------------------------

def test_submit_lead_without_project_key_returns_422() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={"session_id": "s1", "phone": "0905123456", "consent": True},
    )
    assert response.status_code == 422  # G1: project_key BẮT BUỘC
    assert repo.leads == {}


def test_submit_lead_with_invalid_project_key_returns_422() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={"project_key": "Camelia!", "phone": "0905123456", "consent": True},
    )
    assert response.status_code == 422
    assert repo.leads == {}


def test_submit_lead_with_reserved_project_key_returns_422() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={"project_key": "_legacy", "phone": "0905123456", "consent": True},
    )
    assert response.status_code == 422
    assert repo.leads == {}


def test_submit_lead_persists_project_key_and_device_id() -> None:
    client, repo = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "soleil",
            "device_id": "6f2f9a1e-0c4b-4c1e-9b3a-2e1a4b5c6d7e",
            "session_id": "s2",
            "phone": "0905123456",
            "consent": True,
        },
    )
    assert response.status_code == 201
    assert repo.leads[1].project_key == "soleil"
    assert repo.leads[1].device_id == "6f2f9a1e-0c4b-4c1e-9b3a-2e1a4b5c6d7e"


def test_submit_lead_handoff_marking_uses_device_prefixed_key() -> None:
    # D7: the chat context lives under f"{device_id}:{session_id}", so the lead
    # submit must mark the same key or the phone_given gate never blocks.
    from api.application.services.conv_state import get_context

    device_id = "abc123-def456"
    session_id = "s3"
    ctx = get_context(session_id, device_id)
    ctx.useful_turns = 1
    client, _ = make_client()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "device_id": device_id,
            "session_id": session_id,
            "phone": "0905123456",
            "consent": True,
        },
    )
    assert response.status_code == 201
    assert ctx.slots.get("phone_given") is True
    assert ctx.state == "handoff_done"
