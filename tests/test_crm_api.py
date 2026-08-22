"""Story 9.3 / ISSUE-09 — CRM endpoints (offline).

Same seams as test_lead_mirror: the real create_app() app with the lead
repository and realtime mirror swapped for in-memory fakes, and the staff
principal injected through dependency_overrides. The role-matrix test at the
bottom goes one layer deeper and drives the REAL require_sales_or_admin
dependency with locally minted RSA ID tokens (test_admin_auth's offline
verifier machinery). No network, no Firestore, no PG.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.application.services.lead_mirror_service import compute_customer_id
from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.ports.leads import LeadRow, get_lead_repository
from api.infrastructure.ports.realtime_mirror import get_realtime_lead_mirror
from api.interfaces.api import deps as admin_deps
from api.interfaces.api.deps import AuthenticatedPrincipal, require_sales_or_admin
from api.interfaces.api.main import create_app
from tests.test_admin_auth import (
    ISSUER,
    PROJECT_ID,
    _base_claims,
    _build_offline_verifier,
    _mint_id_token,
)
from tests.test_lead_mirror import RecordingLeadMirror
from tests.test_sales_api import FakeLeadRepository

CUSTOMER_PHONE = "0905123456"
CUSTOMER_ID = compute_customer_id(CUSTOMER_PHONE)

ADMIN_PRINCIPAL = AuthenticatedPrincipal(
    firebase_uid="uid-admin", email="admin@example.com", role="admin", sales_id=None
)
ASSIGNED_SALES_PRINCIPAL = AuthenticatedPrincipal(
    firebase_uid="key-1", email="s1@example.com", role="sales", sales_id=1
)
OTHER_SALES_PRINCIPAL = AuthenticatedPrincipal(
    firebase_uid="key-2", email="s2@example.com", role="sales", sales_id=2
)


class FakeCrmLeadRepository(FakeLeadRepository):
    """Sales-API fake extended with the story 9.3 CRM read/write seam.

    customer_id lookups recompute compute_customer_id in Python — the exact
    same digest the PG adapter derives in SQL via pgcrypto."""

    async def get_leads_by_phone(self, phone: str) -> list[LeadRow]:
        matched = [lead for lead in self.leads.values() if lead.phone == phone]
        return sorted(matched, key=lambda lead: lead.created_at, reverse=True)

    async def get_leads_by_customer_id(self, customer_id: str) -> list[LeadRow]:
        matched = [
            lead
            for lead in self.leads.values()
            if compute_customer_id(lead.phone) == customer_id
        ]
        return sorted(matched, key=lambda lead: lead.created_at, reverse=True)

    async def update_lead_crm_state(
        self,
        lead_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        reengage_at: datetime | None = None,
    ) -> LeadRow | None:
        lead = self.leads.get(lead_id)
        if lead is None:
            return None
        lead = replace(
            lead,
            status=status,
            rejection_reason=rejection_reason,
            reengage_at=reengage_at,
            last_action_at=datetime.now(),
            mirror_status="pending",
        )
        self.leads[lead_id] = lead
        return lead

    async def set_marketing_consent_withdrawn_for_customer(
        self, customer_id: str
    ) -> list[LeadRow]:
        withdrawn: list[LeadRow] = []
        withdrawal_stamp = datetime.now()
        for lead in list(self.leads.values()):
            if compute_customer_id(lead.phone) != customer_id:
                continue
            lead = replace(
                lead,
                marketing_withdrawn_at=withdrawal_stamp,
                consent_marketing=False,
                mirror_status="pending",
            )
            self.leads[lead.id] = lead
            withdrawn.append(lead)
        return withdrawn


def seed_lead(
    repo: FakeCrmLeadRepository,
    lead_id: int,
    *,
    phone: str = CUSTOMER_PHONE,
    status: str = "assigned",
    assigned_sales_id: int | None = 1,
    consent_marketing: bool = True,
    minutes_ago: int = 0,
) -> LeadRow:
    lead = LeadRow(
        id=lead_id,
        session_id=f"session-{lead_id}",
        project_key="camellia",
        device_id=None,
        name="Anh Test",
        phone=phone,
        consent=True,
        note=None,
        budget_vnd=None,
        created_at=datetime.now() - timedelta(minutes=minutes_ago),
        status=status,
        assigned_sales_id=assigned_sales_id,
        lock_expires_at=None,
        escal_count=0,
        last_action_at=None,
        closed_at=None,
        consent_service=True,
        consent_marketing=consent_marketing,
        consent_at=datetime.now(),
    )
    repo.leads[lead_id] = lead
    return lead


def make_crm_client(
    repo: FakeCrmLeadRepository,
    mirror: RecordingLeadMirror,
    principal: AuthenticatedPrincipal | None = ASSIGNED_SALES_PRINCIPAL,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: mirror
    if principal is not None:
        app.dependency_overrides[require_sales_or_admin] = lambda: principal
    return TestClient(app)


# ----- GET /api/crm/customers/search -----

def test_search_returns_customer_id_and_masked_lead_history() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 11, minutes_ago=30)
    seed_lead(repo, 12, minutes_ago=5, status="callback")
    client = make_crm_client(repo, RecordingLeadMirror())

    response = client.get("/api/crm/customers/search", params={"phone": "0905 123 456"})

    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == CUSTOMER_ID
    assert body["masked_phone"] == "0905***456"
    assert [lead["lead_id"] for lead in body["leads"]] == [12, 11]
    assert all(lead["masked_phone"] == "0905***456" for lead in body["leads"])
    # PII minimality: the raw number must not appear anywhere in the payload.
    assert CUSTOMER_PHONE not in response.text


def test_search_unknown_phone_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.get(
        "/api/crm/customers/search", params={"phone": "0913999888"}
    ).status_code == 404


def test_search_malformed_phone_is_422() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.get(
        "/api/crm/customers/search", params={"phone": "1234567890"}
    ).status_code == 422


# ----- GET /api/crm/customers/{customer_id}/phone -----

def test_phone_reveal_allowed_for_assigned_sales_and_admin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 21)
    mirror = RecordingLeadMirror()

    with caplog.at_level(logging.INFO, logger="api.crm_customer_service"):
        owner_response = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL).get(
            f"/api/crm/customers/{CUSTOMER_ID}/phone"
        )
        admin_response = make_crm_client(repo, mirror, ADMIN_PRINCIPAL).get(
            f"/api/crm/customers/{CUSTOMER_ID}/phone"
        )

    assert owner_response.status_code == 200
    assert owner_response.json() == {"customer_id": CUSTOMER_ID, "phone": CUSTOMER_PHONE}
    assert admin_response.status_code == 200
    assert admin_response.json()["phone"] == CUSTOMER_PHONE
    # Every reveal emits one audit line carrying actor + customer_id + lead
    # ids — and never the number itself.
    reveal_lines = [
        record for record in caplog.records if "crm.customer_phone_revealed" in record.message
    ]
    assert len(reveal_lines) == 2
    assert CUSTOMER_PHONE not in caplog.text
    assert CUSTOMER_ID in caplog.text


def test_phone_reveal_denied_for_other_sales() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 22)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)
    assert client.get(f"/api/crm/customers/{CUSTOMER_ID}/phone").status_code == 403


def test_phone_reveal_unknown_customer_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    unknown_customer_id = compute_customer_id("0913999888")
    assert client.get(
        f"/api/crm/customers/{unknown_customer_id}/phone"
    ).status_code == 404


# ----- PATCH /api/crm/leads/{lead_id}/status -----

def test_status_patch_persists_and_refreshes_mirror() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 31)
    mirror = RecordingLeadMirror()
    reengage_at = "2026-09-01T09:00:00Z"
    client = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL)

    response = client.patch(
        "/api/crm/leads/31/status",
        json={"status": "lost", "rejection_reason": "Khách không nghe máy", "reengage_at": reengage_at},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["lead_id"] == 31
    assert body["lead_status"] == "lost"
    assert body["mirror_status"] == "done"
    assert body["lead"]["masked_phone"] == "0905***456"
    # PG row persisted the full CRM state...
    persisted = repo.leads[31]
    assert persisted.status == "lost"
    assert persisted.rejection_reason == "Khách không nghe máy"
    assert persisted.reengage_at is not None
    # ...and the realtime mirror document was pushed with the new status.
    assert mirror.upsert_calls == 1
    assert mirror.documents[CUSTOMER_ID].lead_status == "lost"
    assert persisted.mirror_status == "done"


def test_status_patch_denied_for_other_sales() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 32)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)
    assert client.patch(
        "/api/crm/leads/32/status", json={"status": "booked"}
    ).status_code == 403


def test_status_patch_unknown_lead_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.patch(
        "/api/crm/leads/9999/status", json={"status": "booked"}
    ).status_code == 404


def test_status_patch_rejects_status_outside_broker_state_machine() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.patch(
        "/api/crm/leads/1/status", json={"status": "new"}
    ).status_code == 422


# ----- POST /api/crm/customers/{customer_id}/withdraw-marketing-consent -----

def test_withdraw_consent_stamps_timestamp_and_mirrors_all_customer_leads() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 41, minutes_ago=10)
    seed_lead(repo, 42, minutes_ago=2, assigned_sales_id=1)
    seed_lead(repo, 43, phone="0913111222", assigned_sales_id=2)
    mirror = RecordingLeadMirror()
    client = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL)

    response = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["customer_id"] == CUSTOMER_ID
    assert sorted(body["updated_lead_ids"]) == [41, 42]
    # Both of the customer's leads are stamped + flag-flipped; the other
    # customer's lead is untouched.
    assert repo.leads[41].marketing_withdrawn_at is not None
    assert repo.leads[42].marketing_withdrawn_at is not None
    assert repo.leads[41].consent_marketing is False
    assert repo.leads[43].marketing_withdrawn_at is None
    # Realtime clients see the consent flip on the mirror document.
    assert mirror.upsert_calls == 2
    assert mirror.documents[CUSTOMER_ID].consent_marketing is False


def test_withdraw_consent_denied_for_non_owner_sales() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 44)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)
    assert client.post(
        f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent"
    ).status_code == 403


def test_withdraw_consent_unknown_customer_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    unknown_customer_id = compute_customer_id("0913999888")
    assert client.post(
        f"/api/crm/customers/{unknown_customer_id}/withdraw-marketing-consent"
    ).status_code == 404


# ----- Role matrix through the REAL require_sales_or_admin dependency -----

@pytest.fixture(scope="module")
def local_rsa_jwk() -> dict:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-crm-test-key"
    jwk_entry["_private_key"] = private_key
    return jwk_entry


@pytest.fixture()
def matrix_app(
    monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict
) -> FastAPI:
    """Real app + real dependency; only the verifier and the PG sales mapping
    are fakes (same seams as test_admin_auth)."""
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 51)

    verifier_instance = _build_offline_verifier(local_rsa_jwk)
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: verifier_instance,
    )
    monkeypatch.setattr(
        admin_deps.admin,
        "_fetch_active_sales_id_sync",
        lambda firebase_uid: {"key-1": 1, "uid-admin": None}.get(firebase_uid),
    )
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: RecordingLeadMirror()
    return app


def _bearer(local_rsa_jwk: dict, *, firebase_uid: str, role: str) -> dict[str, str]:
    id_token = _mint_id_token(local_rsa_jwk, _base_claims(firebase_uid, role))
    return {"Authorization": f"Bearer {id_token}"}


def test_role_matrix_admin_sales_allowed_viewer_and_anonymous_rejected(
    matrix_app: FastAPI, local_rsa_jwk: dict
) -> None:
    client = TestClient(matrix_app)
    search_path = "/api/crm/customers/search"

    anonymous = client.get(search_path, params={"phone": CUSTOMER_PHONE})
    assert anonymous.status_code == 401

    viewer = client.get(
        search_path,
        params={"phone": CUSTOMER_PHONE},
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-viewer", role="viewer"),
    )
    assert viewer.status_code == 403

    sales = client.get(
        search_path,
        params={"phone": CUSTOMER_PHONE},
        headers=_bearer(local_rsa_jwk, firebase_uid="key-1", role="sales"),
    )
    assert sales.status_code == 200
    assert sales.json()["customer_id"] == CUSTOMER_ID

    admin = client.get(
        search_path,
        params={"phone": CUSTOMER_PHONE},
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-admin", role="admin"),
    )
    assert admin.status_code == 200

    # Unauthenticated mutation is rejected before any business logic runs.
    unauthenticated_patch = client.patch(
        "/api/crm/leads/51/status", json={"status": "booked"}
    )
    assert unauthenticated_patch.status_code == 401
