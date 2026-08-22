"""Story 9.5 / ISSUE-11 — durable staff audit log + role wiring (offline).

The role matrix itself is proven by tests/test_admin_auth.py (claims-based,
hand-minted tokens); this suite proves the audit half: every successful
BE-mediated staff mutation records exactly one durable entry addressed
without raw PII, denied operations record nothing, and a failing store never
breaks the business operation it observes.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from api.application.services.crm_customer_service import (
    reveal_customer_phone_for_assigned_sales_only,
    update_lead_crm_status_and_mirror,
    withdraw_customer_marketing_consent_and_mirror,
)
from api.application.services.lead_mirror_service import compute_customer_id
from api.application.services.staff_audit_service import record_staff_action
from api.application.ports.staff_audit import (
    STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED,
    STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN,
    STAFF_AUDIT_ACTION_PHONE_REVEALED,
    StaffAuditEntry,
)
from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.ports.leads import get_lead_repository
from api.infrastructure.ports.realtime_mirror import get_realtime_lead_mirror
from api.interfaces.api.deps import require_sales_or_admin
from api.interfaces.api.main import create_app
from tests.test_admin_auth import (
    ISSUER,
    PROJECT_ID,
    _base_claims,
    _mint_id_token,
)
from tests.test_crm_api import (
    ASSIGNED_SALES_PRINCIPAL,
    CUSTOMER_PHONE,
    FakeCrmLeadRepository,
    seed_lead,
)
from tests.test_lead_mirror import RecordingLeadMirror


class RecordingStaffAuditStore:
    """In-memory StaffAuditStore capturing every entry for assertions."""

    def __init__(self) -> None:
        self.entries: list[StaffAuditEntry] = []

    async def record_entry(self, entry: StaffAuditEntry) -> None:
        self.entries.append(entry)


class ExplodingStaffAuditStore:
    """Store whose write always fails — auditing must degrade, not raise."""

    async def record_entry(self, entry: StaffAuditEntry) -> None:
        raise RuntimeError("audit sink down")


def _principal(**overrides):
    defaults = dict(
        firebase_uid="uid-sales-9",
        email="s9@example.com",
        role="sales",
        sales_id=9,
    )
    defaults.update(overrides)
    from api.interfaces.api.deps import AuthenticatedPrincipal

    return AuthenticatedPrincipal(**defaults)


# ---------------------------------------------------------------------------
# staff_audit_service.record_staff_action semantics
# ---------------------------------------------------------------------------


def test_record_staff_action_persists_entry_built_from_principal() -> None:
    store = RecordingStaffAuditStore()
    principal = _principal()

    recorded = asyncio.run(
        record_staff_action(
            store,
            principal=principal,
            action=STAFF_AUDIT_ACTION_PHONE_REVEALED,
            customer_id="cust-digest",
            detail={"revealed_lead_ids": [1, 2]},
        )
    )

    assert recorded is True
    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.actor_firebase_uid == "uid-sales-9"
    assert entry.actor_role == "sales"
    assert entry.actor_sales_id == 9
    assert entry.action == STAFF_AUDIT_ACTION_PHONE_REVEALED
    assert entry.customer_id == "cust-digest"
    assert entry.detail == {"revealed_lead_ids": [1, 2]}


def test_record_staff_action_without_store_degrades_to_logger_only() -> None:
    recorded = asyncio.run(
        record_staff_action(
            None,
            principal=_principal(),
            action=STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED,
        )
    )
    assert recorded is False


def test_record_staff_action_survives_store_failure() -> None:
    recorded = asyncio.run(
        record_staff_action(
            ExplodingStaffAuditStore(),
            principal=_principal(),
            action=STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED,
        )
    )
    # The audited mutation must not fail because its trail could not be written.
    assert recorded is False


# ---------------------------------------------------------------------------
# CRM service mutations emit durable entries (no raw PII anywhere)
# ---------------------------------------------------------------------------


def _seeded_repo() -> FakeCrmLeadRepository:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 1, phone=CUSTOMER_PHONE, status="assigned", assigned_sales_id=1)
    seed_lead(repo, 2, phone=CUSTOMER_PHONE, status="called", assigned_sales_id=1)
    return repo


def test_reveal_records_entry_without_the_raw_phone() -> None:
    repo = _seeded_repo()
    store = RecordingStaffAuditStore()

    asyncio.run(
        reveal_customer_phone_for_assigned_sales_only(
            repo,
            customer_id=compute_customer_id(CUSTOMER_PHONE),
            principal=ASSIGNED_SALES_PRINCIPAL,
            audit_store=store,
        )
    )

    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.action == STAFF_AUDIT_ACTION_PHONE_REVEALED
    assert entry.customer_id == compute_customer_id(CUSTOMER_PHONE)
    assert sorted(entry.detail["revealed_lead_ids"]) == [1, 2]
    # The audit payload must not become a secondary PII leak channel.
    serialized = json.dumps(
        {
            **entry.detail,
            "customer_id": entry.customer_id,
            "actor_firebase_uid": entry.actor_firebase_uid,
        }
    )
    assert CUSTOMER_PHONE not in serialized


def test_status_update_records_old_and_new_status() -> None:
    repo = _seeded_repo()
    store = RecordingStaffAuditStore()
    mirror = RecordingLeadMirror()

    asyncio.run(
        update_lead_crm_status_and_mirror(
            repo,
            mirror,
            lead_id=1,
            status="booked",
            rejection_reason=None,
            reengage_at=None,
            principal=ASSIGNED_SALES_PRINCIPAL,
            audit_store=store,
        )
    )

    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.action == STAFF_AUDIT_ACTION_LEAD_STATUS_UPDATED
    assert entry.lead_id == 1
    assert entry.customer_id == compute_customer_id(CUSTOMER_PHONE)
    assert entry.detail == {"old_status": "assigned", "new_status": "booked"}


def test_marketing_withdrawal_records_customer_scoped_entry() -> None:
    repo = _seeded_repo()
    store = RecordingStaffAuditStore()
    mirror = RecordingLeadMirror()

    withdrawn_leads = asyncio.run(
        withdraw_customer_marketing_consent_and_mirror(
            repo,
            mirror,
            customer_id=compute_customer_id(CUSTOMER_PHONE),
            principal=ASSIGNED_SALES_PRINCIPAL,
            audit_store=store,
        )
    )

    assert len(withdrawn_leads) == 2
    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.action == STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN
    assert entry.customer_id == compute_customer_id(CUSTOMER_PHONE)
    assert sorted(entry.detail["withdrawn_lead_ids"]) == [1, 2]


# ---------------------------------------------------------------------------
# Route-level: real create_app, offline RSA tokens (hand-created-account path)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def local_rsa_jwk() -> dict:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-test-key"
    jwk_entry["_private_key"] = private_key
    return jwk_entry


@pytest.fixture(autouse=True)
def offline_auth_seams(monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict) -> None:
    async def fake_key_for_kid(kid: str):
        if kid != local_rsa_jwk["kid"]:
            return None
        jwk_entry = {k: v for k, v in local_rsa_jwk.items() if not k.startswith("_")}
        from jwt.algorithms import RSAAlgorithm

        return RSAAlgorithm.from_jwk(jwk_entry)

    from api.infrastructure.adapters import firebase_auth_jwks

    verifier_instance = firebase_auth_jwks.FirebaseAuthJwksVerifier(
        project_id=PROJECT_ID,
        jwks_url="https://example.invalid/jwks",
        issuer=ISSUER,
        audience=PROJECT_ID,
    )
    verifier_instance._key_for_kid = fake_key_for_kid  # type: ignore[method-assign]
    monkeypatch.setattr(
        dependency_injection, "get_firebase_auth_verifier", lambda: verifier_instance
    )

    def fake_fetch_active_sales_id_sync(firebase_uid: str) -> int | None:
        # No token maps to a PG sales row in this suite — the admin acts with
        # sales_id=None and foreign-sales callers fail ownership checks.
        return None

    from api.interfaces.api import deps as admin_deps

    monkeypatch.setattr(
        admin_deps.admin, "_fetch_active_sales_id_sync", fake_fetch_active_sales_id_sync
    )


def _bearer(local_rsa_jwk: dict, *, firebase_uid: str, role: str) -> dict[str, str]:
    token = _mint_id_token(local_rsa_jwk, _base_claims(firebase_uid, role))
    return {"Authorization": f"Bearer {token}"}


def _client_with_seams(store: RecordingStaffAuditStore) -> TestClient:
    repo = _seeded_repo()
    mirror = RecordingLeadMirror()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: mirror
    from api.infrastructure.dependencies import get_staff_audit_store

    app.dependency_overrides[get_staff_audit_store] = lambda: store
    return TestClient(app)


def test_admin_status_patch_through_route_writes_audit_entry(
    local_rsa_jwk: dict,
) -> None:
    store = RecordingStaffAuditStore()
    client = _client_with_seams(store)

    response = client.patch(
        "/api/crm/leads/1/status",
        json={"status": "booked"},
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-admin-x", role="admin"),
    )

    assert response.status_code == 200, response.text
    assert len(store.entries) == 1
    entry = store.entries[0]
    assert entry.actor_firebase_uid == "uid-admin-x"
    assert entry.actor_role == "admin"
    assert entry.detail == {"old_status": "assigned", "new_status": "booked"}


def test_denied_mutation_records_nothing(local_rsa_jwk: dict) -> None:
    store = RecordingStaffAuditStore()
    client = _client_with_seams(store)

    response = client.patch(
        "/api/crm/leads/1/status",
        json={"status": "booked"},
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-other", role="sales"),
    )

    # Sales B touching sales A's lead is denied AND leaves no audit write —
    # only *successful* staff actions belong in the trail.
    assert response.status_code == 403
    assert store.entries == []
