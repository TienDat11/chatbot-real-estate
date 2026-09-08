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

from api.application.ports.staff_audit import STAFF_AUDIT_ACTION_PHONE_REVEALED
from api.application.services.chat_history_service import ChatMessage
from api.application.services.crm_customer_service import (
    CrmLeadAccessDeniedError,
    _ensure_principal_owns_lead,
    principal_is_assigned_owner_of_any_lead,
)
from api.application.services.lead_mirror_service import (
    compute_customer_id,
    compute_lead_document_id,
)
from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.dependencies import get_reengage_queue_store
from api.infrastructure.ports.leads import LeadRow, get_lead_repository
from api.infrastructure.ports.realtime_mirror import get_realtime_lead_mirror
from api.interfaces.api import deps as admin_deps
from api.interfaces.api.deps import AuthenticatedPrincipal, require_sales_or_admin
from api.interfaces.api.main import create_app
from tests.test_admin_auth import (
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


@pytest.fixture(autouse=True)
def _reset_reveal_rate_limit():
    """G3-r6: the reveal limiter is a process singleton; isolate tests from it."""
    from api.application.services.phone_reveal_rate_limit import reveal_rate_limiter

    reveal_rate_limiter.reset()
    yield
    reveal_rate_limiter.reset()


class FakeCrmLeadRepository(FakeLeadRepository):
    """Sales-API fake extended with the story 9.3 CRM read/write seam.

    customer_id lookups recompute compute_customer_id in Python — the exact
    same digest the PG adapter derives in SQL via pgcrypto."""

    async def get_leads_by_phone(self, phone: str) -> list[LeadRow]:
        matched = [lead for lead in self.leads.values() if lead.phone == phone]
        return sorted(matched, key=lambda lead: lead.created_at, reverse=True)

    async def get_leads_by_customer_id(self, customer_id: str) -> list[LeadRow]:
        matched = [
            lead for lead in self.leads.values() if compute_customer_id(lead.phone) == customer_id
        ]
        return sorted(matched, key=lambda lead: lead.created_at, reverse=True)

    async def update_lead_crm_state(
        self,
        lead_id: int,
        *,
        status: str,
        rejection_reason: str | None = None,
        reengage_at: datetime | None = None,
        assigned_sales_id: int | None = None,
        admin_authorized: bool = False,
    ) -> LeadRow | None:
        lead = self.leads.get(lead_id)
        if lead is None or (not admin_authorized and lead.assigned_sales_id != assigned_sales_id):
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
        self,
        customer_id: str,
        *,
        assigned_sales_id: int | None = None,
        admin_authorized: bool = False,
    ) -> list[LeadRow]:
        # Mirrors the PG adapter's atomic gate: a non-admin must own at least
        # one of the customer's rows to trigger the opt-out, but the opt-out
        # itself applies to EVERY row sharing the customer id — including rows
        # assigned to other sales. Returns [] when the caller lost ownership
        # (or the customer has no rows) so the service can 403/404.
        customer_leads = [
            lead
            for lead in self.leads.values()
            if compute_customer_id(lead.phone) == customer_id
        ]
        if not customer_leads:
            return []
        if not admin_authorized and not any(
            lead.assigned_sales_id == assigned_sales_id for lead in customer_leads
        ):
            return []
        withdrawn: list[LeadRow] = []
        for lead in customer_leads:
            lead = replace(
                lead,
                marketing_withdrawn_at=lead.marketing_withdrawn_at or datetime.now(),
                consent_marketing=False,
                reengage_at=None,
                mirror_status="pending",
            )
            self.leads[lead.id] = lead
            withdrawn.append(lead)
        return withdrawn


class RecordingReengageQueueStore:
    """In-memory ReengageQueueStore recording cancellations for assertions."""

    def __init__(self) -> None:
        self.cancelled_customer_ids: list[str] = []

    async def save_queue_entries(self, entries) -> None:
        return None

    async def load_attempt_counts_by_customer_id(self) -> dict[str, int]:
        return {}

    async def cancel_queue_entries_for_customer(self, customer_id: str) -> None:
        self.cancelled_customer_ids.append(customer_id)


def seed_lead(
    repo: FakeCrmLeadRepository,
    lead_id: int,
    *,
    phone: str = CUSTOMER_PHONE,
    status: str = "assigned",
    assigned_sales_id: int | None = 1,
    consent_marketing: bool = True,
    minutes_ago: int = 0,
    name: str = "Anh Test",
    reengage_at_marker: bool = False,
) -> LeadRow:
    lead = LeadRow(
        id=lead_id,
        session_id=f"session-{lead_id}",
        project_key="camellia",
        device_id=None,
        name=name,
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
        reengage_at=datetime.now() + timedelta(days=1) if reengage_at_marker else None,
    )
    repo.leads[lead_id] = lead
    return lead


def make_crm_client(
    repo: FakeCrmLeadRepository,
    mirror: RecordingLeadMirror,
    principal: AuthenticatedPrincipal | None = ASSIGNED_SALES_PRINCIPAL,
    audit_store=None,
    queue_store: RecordingReengageQueueStore | None = None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: mirror
    if queue_store is not None:
        app.dependency_overrides[get_reengage_queue_store] = lambda: queue_store
    # Story 9.5: mutations now audit through the PG store — swap in an
    # in-memory recorder so offline tests never touch a database.
    from api.infrastructure.dependencies import get_staff_audit_store
    from tests.test_staff_audit import RecordingStaffAuditStore

    audit_store = audit_store or RecordingStaffAuditStore()
    app.dependency_overrides[get_staff_audit_store] = lambda: audit_store
    if principal is not None:
        app.dependency_overrides[require_sales_or_admin] = lambda: principal
    return TestClient(app)


# ----- GET /api/crm/customers/search -----


def test_search_returns_customer_id_and_masked_lead_history() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 11, minutes_ago=30)
    seed_lead(repo, 12, minutes_ago=5, status="callback")
    client = make_crm_client(repo, RecordingLeadMirror())

    response = client.post("/api/crm/customers/search", json={"phone": "0905 123 456"})

    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == CUSTOMER_ID
    assert body["masked_phone"] == "0905***456"
    assert [lead["lead_id"] for lead in body["leads"]] == [12, 11]
    assert [lead["id"] for lead in body["leads"]] == [CUSTOMER_ID, CUSTOMER_ID]
    assert all(lead["masked_phone"] == "0905***456" for lead in body["leads"])
    # PII minimality: the raw number must not appear anywhere in the payload.
    assert CUSTOMER_PHONE not in response.text


@pytest.mark.parametrize(
    ("principal", "expected_lead_ids"),
    [
        (ASSIGNED_SALES_PRINCIPAL, [101]),
        (OTHER_SALES_PRINCIPAL, [102]),
        (ADMIN_PRINCIPAL, [103, 102, 101]),
    ],
)
def test_search_discloses_only_owned_leads_or_all_leads_to_admin(
    principal: AuthenticatedPrincipal, expected_lead_ids: list[int]
) -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 101, assigned_sales_id=1, name="Owner lead", minutes_ago=30)
    seed_lead(repo, 102, assigned_sales_id=2, name="Other lead", minutes_ago=20)
    seed_lead(repo, 103, assigned_sales_id=None, name="Unassigned lead", minutes_ago=10)
    client = make_crm_client(repo, RecordingLeadMirror(), principal)

    response = client.post("/api/crm/customers/search", json={"phone": CUSTOMER_PHONE})

    assert response.status_code == 200
    leads = response.json()["leads"]
    assert [lead["lead_id"] for lead in leads] == expected_lead_ids
    if principal.role != "admin":
        assert all(lead["assigned_sales_id"] == principal.sales_id for lead in leads)
        assert all(lead["display_name"] != "Unassigned lead" for lead in leads)


def test_search_with_only_unassigned_or_other_sales_leads_is_404() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 104, assigned_sales_id=2, name="Other lead")
    seed_lead(repo, 105, assigned_sales_id=None, name="Unassigned lead")
    client = make_crm_client(repo, RecordingLeadMirror(), ASSIGNED_SALES_PRINCIPAL)

    response = client.post("/api/crm/customers/search", json={"phone": CUSTOMER_PHONE})

    assert response.status_code == 404


def test_search_unknown_phone_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.post("/api/crm/customers/search", json={"phone": "0913999888"}).status_code == 404


def test_search_malformed_phone_is_422() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    response = client.post("/api/crm/customers/search", json={"phone": "1234567890"})
    assert response.status_code == 422
    assert "1234567890" not in response.text


def test_search_requires_json_body_and_legacy_get_does_not_accept_phone() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    missing_body = client.post("/api/crm/customers/search")
    legacy = client.get("/api/crm/customers/search", params={"phone": CUSTOMER_PHONE})
    assert missing_body.status_code == 422
    assert CUSTOMER_PHONE not in missing_body.text
    assert legacy.status_code == 410
    assert CUSTOMER_PHONE not in legacy.text
    assert legacy.headers["deprecation"] == "true"


def test_search_does_not_log_raw_phone(caplog: pytest.LogCaptureFixture) -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 106)
    client = make_crm_client(repo, RecordingLeadMirror())
    with caplog.at_level(logging.INFO):
        response = client.post("/api/crm/customers/search", json={"phone": CUSTOMER_PHONE})
    assert response.status_code == 200
    assert CUSTOMER_PHONE not in caplog.text


# ----- Ownership fail-closed regressions -----


def test_sales_without_sales_id_cannot_claim_unassigned_lead() -> None:
    repo = FakeCrmLeadRepository()
    unassigned_lead = seed_lead(repo, 20, assigned_sales_id=None)
    principal = AuthenticatedPrincipal(
        firebase_uid="uid-unmapped",
        email="unmapped@example.com",
        role="sales",
        sales_id=None,
    )

    assert principal_is_assigned_owner_of_any_lead(principal, [unassigned_lead]) is False
    with pytest.raises(CrmLeadAccessDeniedError):
        _ensure_principal_owns_lead(principal, unassigned_lead)


def test_assigned_sales_principal_still_owns_assigned_lead() -> None:
    repo = FakeCrmLeadRepository()
    assigned_lead = seed_lead(repo, 21, assigned_sales_id=1)

    assert (
        principal_is_assigned_owner_of_any_lead(ASSIGNED_SALES_PRINCIPAL, [assigned_lead]) is True
    )
    _ensure_principal_owns_lead(ASSIGNED_SALES_PRINCIPAL, assigned_lead)


# ----- GET /api/crm/customers/{customer_id}/phone -----


def test_phone_reveal_allowed_for_assigned_sales_and_admin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 21)
    mirror = RecordingLeadMirror()

    # Story 9.5 / G3-r6: every reveal emits one audit line carrying actor +
    # customer_id + lead ids — and never the number itself. The reveal trail
    # now flows through the dedicated allowlisted recorder (logger
    # api.phone_reveal_audit), which also stamps the correlation id.
    with caplog.at_level(logging.INFO, logger="api.phone_reveal_audit"):
        owner_response = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL).get(
            f"/api/crm/customers/{CUSTOMER_ID}/phone", headers={"X-Correlation-ID": "corr-9"}
        )
        admin_response = make_crm_client(repo, mirror, ADMIN_PRINCIPAL).get(
            f"/api/crm/customers/{CUSTOMER_ID}/phone"
        )

    assert owner_response.status_code == 200
    assert owner_response.json() == {"customer_id": CUSTOMER_ID, "phone": CUSTOMER_PHONE}
    assert admin_response.status_code == 200
    assert admin_response.json()["phone"] == CUSTOMER_PHONE
    # G3-r6 §10.2 headers: client correlation echoed, private no-store on PII.
    assert owner_response.headers["X-Correlation-ID"] == "corr-9"
    assert owner_response.headers["Cache-Control"] == "no-store, private"
    assert owner_response.headers["Vary"] == "Authorization"
    reveal_lines = [
        record
        for record in caplog.records
        if f"action={STAFF_AUDIT_ACTION_PHONE_REVEALED}" in record.message
    ]
    assert len(reveal_lines) == 2
    assert CUSTOMER_PHONE not in caplog.text
    assert CUSTOMER_ID in caplog.text


def test_phone_reveal_fails_closed_when_audit_write_fails() -> None:
    from tests.test_staff_audit import ExplodingStaffAuditStore

    repo = FakeCrmLeadRepository()
    seed_lead(repo, 23)
    response = make_crm_client(
        repo, RecordingLeadMirror(), ASSIGNED_SALES_PRINCIPAL, ExplodingStaffAuditStore()
    ).get(f"/api/crm/customers/{CUSTOMER_ID}/phone")
    assert response.status_code == 503
    assert CUSTOMER_PHONE not in response.text
    assert response.headers["Cache-Control"] == "no-store, private"


def test_phone_reveal_denied_for_other_sales() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 22)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)
    assert client.get(f"/api/crm/customers/{CUSTOMER_ID}/phone").status_code == 403


def test_phone_reveal_unknown_customer_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    unknown_customer_id = compute_customer_id("0913999888")
    assert client.get(f"/api/crm/customers/{unknown_customer_id}/phone").status_code == 404


# ----- GET /api/crm/leads/{lead_id}/conversation -----


class FakeChatHistoryRepository:
    def __init__(self, messages=None, sessions=None):
        self.messages = messages or []
        self.sessions = sessions

    async def get_session_for_lead(self, *, lead_id: int):
        if self.sessions is not None:
            return self.sessions.get(lead_id)
        if lead_id != 24:
            return None
        return "session-24", "camellia", self.messages



def test_lead_conversation_returns_safe_transcript_for_owner_and_admin() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 24)
    history = FakeChatHistoryRepository([
        ChatMessage("user", "Giá bao nhiêu?", {"internal": "drop"}, datetime.now()),
        ChatMessage("assistant", "Mời bạn xem thông tin.", {"sources": ["doc-1"]}, datetime.now()),
    ])
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    from api.infrastructure.dependencies import get_chat_history_repository
    app.dependency_overrides[get_chat_history_repository] = lambda: history
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: ASSIGNED_SALES_PRINCIPAL
    response = TestClient(app).get(
        "/api/crm/leads/24/conversation",
        headers={"X-Correlation-ID": "corr-transcript"},
    )
    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "corr-transcript"
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Vary"] == "Authorization"
    assert response.json()["session_id"] == "session-24"
    assert response.json()["messages"][0]["meta"] is None
    assert response.json()["messages"][1]["meta"] == {"sources": ["doc-1"]}


@pytest.mark.parametrize(
    "principal",
    [ASSIGNED_SALES_PRINCIPAL, ADMIN_PRINCIPAL],
    ids=["assigned-sales", "admin"],
)
def test_lead_conversation_returns_linked_session_transcript_for_authorized_roles(
    principal: AuthenticatedPrincipal,
) -> None:
    repo = FakeCrmLeadRepository()
    lead = seed_lead(repo, 177)
    # A lead is entitled to its explicitly linked chat session. An unrelated
    # or absent session must not hydrate a transcript: the empty-state 200 is
    # reserved for a lead that genuinely has no prior chat (see the no-link
    # test below), never for a cross-session read.
    history = FakeChatHistoryRepository(
        sessions={
            lead.id: (
                lead.session_id,
                lead.project_key,
                [ChatMessage("assistant", "Linked transcript", None, datetime.now())],
            )
        }
    )
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    from api.infrastructure.dependencies import get_chat_history_repository
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin

    app.dependency_overrides[get_chat_history_repository] = lambda: history
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: principal

    response = TestClient(app).get(
        f"/api/crm/leads/{lead.id}/conversation",
        headers={"X-Correlation-ID": "corr-linked-session"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == lead.session_id
    assert body["project_key"] == lead.project_key
    assert body["messages"] == [
        {
            "role": "assistant",
            "content": "Linked transcript",
            "meta": None,
            "created_at": body["messages"][0]["created_at"],
        }
    ]
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Vary"] == "Authorization"
    assert response.headers["X-Correlation-ID"] == "corr-linked-session"


def test_lead_conversation_returns_empty_transcript_200_when_lead_has_no_prior_chat() -> None:
    """BE-CRM empty state: an existing legitimate lead whose caller is the
    assigned sales answers 200 with an EMPTY transcript (the CRM renders its
    empty state); only a missing lead 404s and only a non-owner 403s. Private
    no-store headers are preserved and the raw phone never rides the body."""
    repo = FakeCrmLeadRepository()
    lead = seed_lead(repo, 179)
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    from api.infrastructure.dependencies import get_chat_history_repository
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin

    app.dependency_overrides[get_chat_history_repository] = (
        lambda: FakeChatHistoryRepository(sessions={})
    )
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: ASSIGNED_SALES_PRINCIPAL

    response = TestClient(app).get(
        "/api/crm/leads/179/conversation",
        headers={"X-Correlation-ID": "corr-no-link"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] is None
    assert body["project_key"] == lead.project_key
    assert body["messages"] == []
    assert CUSTOMER_PHONE not in response.text
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Vary"] == "Authorization"
    assert response.headers["X-Correlation-ID"] == "corr-no-link"


def test_lead_conversation_empty_state_allowed_for_admin_too() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 180)
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    from api.infrastructure.dependencies import get_chat_history_repository
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin

    app.dependency_overrides[get_chat_history_repository] = (
        lambda: FakeChatHistoryRepository(sessions={})
    )
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: ADMIN_PRINCIPAL

    response = TestClient(app).get("/api/crm/leads/180/conversation")

    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert response.json()["session_id"] is None


def test_lead_conversation_oversized_body_preserves_security_headers() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 24)
    history = FakeChatHistoryRepository(
        [ChatMessage("user", "x" * (1024 * 1024), None, datetime.now())]
    )
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    from api.infrastructure.dependencies import get_chat_history_repository
    app.dependency_overrides[get_chat_history_repository] = lambda: history
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: ASSIGNED_SALES_PRINCIPAL

    response = TestClient(app).get(
        "/api/crm/leads/24/conversation",
        headers={"X-Correlation-ID": "corr-oversized"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Conversation exceeds body limit"}
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Vary"] == "Authorization"
    assert response.headers["X-Correlation-ID"] == "corr-oversized"


def test_lead_conversation_returns_403_for_other_sales_and_404_for_missing() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 25)
    from api.infrastructure.dependencies import get_chat_history_repository
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_chat_history_repository] = lambda: FakeChatHistoryRepository()
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: OTHER_SALES_PRINCIPAL
    client = TestClient(app)
    denied = client.get(
        "/api/crm/leads/25/conversation", headers={"X-Correlation-ID": "corr-denied"}
    )
    assert denied.status_code == 403
    assert denied.headers["X-Correlation-ID"] == "corr-denied"
    assert denied.headers["Cache-Control"] == "no-store, private"
    assert denied.headers["Vary"] == "Authorization"
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: ASSIGNED_SALES_PRINCIPAL
    missing = client.get("/api/crm/leads/999/conversation")
    assert missing.status_code == 404
    assert missing.headers["X-Correlation-ID"].startswith("corr-")
    assert missing.headers["Cache-Control"] == "no-store, private"
    assert missing.headers["Vary"] == "Authorization"


def test_lead_conversation_masks_nested_raw_phones_but_keeps_legitimate_metadata() -> None:
    """Reviewer blocker (PII): a raw phone can no longer ride out through a
    NESTED meta value — facts[].fields, sources[].section, and images[].caption
    are recursively masked; every non-sensitive value stays intact and unknown
    top-level keys are still dropped by the allowlist."""
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 24)
    meta = {
        "facts": [{"fe_id": "f1", "fields": {"hotline": "0912 345 678", "area": 88}}],
        "sources": [
            {"doc_id": "d1", "title": "Bảng giá Camellia", "section": "+84 91 234 5678"}
        ],
        "images": [{"url": "https://cdn.example/x.png", "caption": "Gọi (0912)-345-678"}],
        "internal_only": {"phone": "0912345678"},
    }
    history = FakeChatHistoryRepository(
        [ChatMessage("assistant", "Meta transcript", meta, datetime.now())]
    )
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    from api.infrastructure.dependencies import get_chat_history_repository
    app.dependency_overrides[get_chat_history_repository] = lambda: history
    from api.interfaces.api.crm_routes import _require_transcript_sales_or_admin
    app.dependency_overrides[_require_transcript_sales_or_admin] = lambda: ASSIGNED_SALES_PRINCIPAL

    response = TestClient(app).get(
        "/api/crm/leads/24/conversation", headers={"X-Correlation-ID": "corr-nested-phone"}
    )

    assert response.status_code == 200
    body_meta = response.json()["messages"][0]["meta"]
    # Phones masked under every nested collection.
    assert body_meta["facts"][0]["fields"]["hotline"] == "0912***678"
    assert body_meta["sources"][0]["section"] == "+849***678"
    assert body_meta["images"][0]["caption"] == "Gọi 0912***678"
    # Legitimate non-sensitive metadata is preserved untouched.
    assert body_meta["facts"][0]["fe_id"] == "f1"
    assert body_meta["facts"][0]["fields"]["area"] == 88
    assert body_meta["sources"][0]["doc_id"] == "d1"
    assert body_meta["sources"][0]["title"] == "Bảng giá Camellia"
    assert body_meta["images"][0]["url"] == "https://cdn.example/x.png"
    # Allowlist still drops unknown top-level keys.
    assert "internal_only" not in body_meta
    # Defense in depth: no formatting variant of the raw number survives.
    for variant in ("0912345678", "0912 345 678", "0912-345-678", "912345678"):
        assert variant not in response.text


# ----- PATCH /api/crm/leads/{lead_id}/status -----


def test_status_patch_persists_and_refreshes_mirror() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 31)
    mirror = RecordingLeadMirror()
    reengage_at = "2026-09-01T09:00:00Z"
    client = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL)

    response = client.patch(
        "/api/crm/leads/31/status",
        json={
            "status": "lost",
            "rejection_reason": "Khách không nghe máy",
            "reengage_at": reengage_at,
        },
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
    # ADR-0004: the document id is the per-lead HMAC, not the customer HMAC.
    assert mirror.upsert_calls == 1
    assert mirror.documents[compute_lead_document_id(31)].lead_status == "lost"
    assert persisted.mirror_status == "done"


def test_status_patch_denied_for_other_sales() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 32)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)
    assert client.patch("/api/crm/leads/32/status", json={"status": "booked"}).status_code == 403


def test_status_patch_unknown_lead_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.patch("/api/crm/leads/9999/status", json={"status": "booked"}).status_code == 404


def test_status_patch_rejects_status_outside_broker_state_machine() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    assert client.patch("/api/crm/leads/1/status", json={"status": "new"}).status_code == 422


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
    # Realtime clients see the consent flip on BOTH of the customer's mirror
    # documents — ADR-0004 keys documents per lead, so lead 42 no longer
    # overwrites lead 41's projection (the pre-fix collision).
    assert mirror.upsert_calls == 2
    assert mirror.documents[compute_lead_document_id(41)].consent_marketing is False
    assert mirror.documents[compute_lead_document_id(42)].consent_marketing is False


def test_withdraw_consent_records_audit_event() -> None:
    from api.application.ports.staff_audit import STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN
    from tests.test_staff_audit import RecordingStaffAuditStore

    repo = FakeCrmLeadRepository()
    seed_lead(repo, 40)
    audit_store = RecordingStaffAuditStore()
    client = make_crm_client(repo, RecordingLeadMirror(), ASSIGNED_SALES_PRINCIPAL, audit_store)

    response = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")

    assert response.status_code == 200
    assert len(audit_store.entries) == 1
    entry = audit_store.entries[0]
    assert entry.action == STAFF_AUDIT_ACTION_MARKETING_CONSENT_WITHDRAWN
    assert entry.customer_id == CUSTOMER_ID
    assert entry.detail == {"withdrawn_lead_ids": [40]}


def test_withdraw_consent_denied_for_non_owner_sales() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 44)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)
    assert (
        client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent").status_code
        == 403
    )


def test_withdraw_consent_unknown_customer_is_404() -> None:
    client = make_crm_client(FakeCrmLeadRepository(), RecordingLeadMirror())
    unknown_customer_id = compute_customer_id("0913999888")
    assert (
        client.post(
            f"/api/crm/customers/{unknown_customer_id}/withdraw-marketing-consent"
        ).status_code
        == 404
    )


def test_withdraw_consent_by_one_sales_flips_other_sales_rows_for_same_customer() -> None:
    # The opt-out is CUSTOMER-wide: a sales user who owns one row of the
    # customer withdraws consent for EVERY row of that customer, including
    # rows assigned to other sales.
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 61, assigned_sales_id=1, minutes_ago=10)
    seed_lead(repo, 62, assigned_sales_id=2, minutes_ago=5, reengage_at_marker=True)
    seed_lead(repo, 63, phone="0913111222", assigned_sales_id=2)
    seed_lead(repo, 64, phone="0913111222", assigned_sales_id=1)
    mirror = RecordingLeadMirror()
    client = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL)

    response = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")

    assert response.status_code == 200
    assert sorted(response.json()["updated_lead_ids"]) == [61, 62]
    # Both rows of the SAME customer flipped, regardless of which sales owns
    # them; the other customer's rows (63, 64) are untouched. The open
    # re-approach marker on lead 62 is cancelled with the consent.
    for lead_id in (61, 62):
        assert repo.leads[lead_id].consent_marketing is False
        assert repo.leads[lead_id].marketing_withdrawn_at is not None
    assert repo.leads[62].reengage_at is None
    for lead_id in (63, 64):
        assert repo.leads[lead_id].consent_marketing is True
        assert repo.leads[lead_id].marketing_withdrawn_at is None
    assert mirror.documents[compute_lead_document_id(61)].consent_marketing is False
    assert mirror.documents[compute_lead_document_id(62)].consent_marketing is False
    assert mirror.documents[compute_lead_document_id(62)].reengage_at is None


def test_withdraw_consent_admin_withdraws_every_row_regardless_of_ownership() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 71, assigned_sales_id=1)
    seed_lead(repo, 72, assigned_sales_id=2)
    client = make_crm_client(repo, RecordingLeadMirror(), ADMIN_PRINCIPAL)

    response = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")

    assert response.status_code == 200
    assert sorted(response.json()["updated_lead_ids"]) == [71, 72]
    assert all(repo.leads[i].consent_marketing is False for i in (71, 72))


def test_withdraw_consent_cancels_open_reengage_suggestions_for_customer() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 81)
    queue_store = RecordingReengageQueueStore()
    client = make_crm_client(repo, RecordingLeadMirror(), queue_store=queue_store)

    response = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")

    assert response.status_code == 200
    assert queue_store.cancelled_customer_ids == [CUSTOMER_ID]


def test_withdraw_consent_is_idempotent_and_preserves_first_timestamp() -> None:
    repo = FakeCrmLeadRepository()
    seed_lead(repo, 91, assigned_sales_id=1)
    seed_lead(repo, 92, assigned_sales_id=2)
    mirror = RecordingLeadMirror()
    client = make_crm_client(repo, mirror, ASSIGNED_SALES_PRINCIPAL)

    first = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")
    assert first.status_code == 200
    first_stamp = repo.leads[91].marketing_withdrawn_at
    assert first_stamp is not None

    second = client.post(f"/api/crm/customers/{CUSTOMER_ID}/withdraw-marketing-consent")
    assert second.status_code == 200
    assert sorted(second.json()["updated_lead_ids"]) == [91, 92]
    # Repeat is safe and does not move the legal withdrawal timestamp.
    assert repo.leads[91].marketing_withdrawn_at == first_stamp
    assert repo.leads[92].marketing_withdrawn_at == first_stamp
    assert repo.leads[91].consent_marketing is False
    assert repo.leads[92].consent_marketing is False


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
def matrix_app(monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict) -> FastAPI:
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

    anonymous = client.post(search_path, json={"phone": CUSTOMER_PHONE})
    assert anonymous.status_code == 401

    viewer = client.post(
        search_path,
        json={"phone": CUSTOMER_PHONE},
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-viewer", role="viewer"),
    )
    assert viewer.status_code == 403

    sales = client.post(
        search_path,
        json={"phone": CUSTOMER_PHONE},
        headers=_bearer(local_rsa_jwk, firebase_uid="key-1", role="sales"),
    )
    assert sales.status_code == 200
    assert sales.json()["customer_id"] == CUSTOMER_ID

    admin = client.post(
        search_path,
        json={"phone": CUSTOMER_PHONE},
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-admin", role="admin"),
    )
    assert admin.status_code == 200

    # Unauthenticated mutation is rejected before any business logic runs.
    unauthenticated_patch = client.patch("/api/crm/leads/51/status", json={"status": "booked"})
    assert unauthenticated_patch.status_code == 401


def test_transcript_real_auth_rejects_anonymous_and_viewer_with_security_headers(
    matrix_app: FastAPI, local_rsa_jwk: dict
) -> None:
    """Transcript-specific auth wrapper retains private error response headers."""
    client = TestClient(matrix_app)
    path = "/api/crm/leads/51/conversation"

    anonymous = client.get(path, headers={"X-Correlation-ID": "corr-anonymous"})
    viewer = client.get(
        path,
        headers={
            **_bearer(local_rsa_jwk, firebase_uid="uid-viewer", role="viewer"),
            "X-Correlation-ID": "corr-viewer",
        },
    )

    for response, status, correlation_id in (
        (anonymous, 401, "corr-anonymous"),
        (viewer, 403, "corr-viewer"),
    ):
        assert response.status_code == status
        assert response.json() == {"detail": "Authorization failed"}
        assert response.headers["Cache-Control"] == "no-store, private"
        assert response.headers["Vary"] == "Authorization"
        assert response.headers["X-Correlation-ID"] == correlation_id


def test_transcript_real_auth_allows_mapped_sales_token_end_to_end(
    matrix_app: FastAPI, local_rsa_jwk: dict
) -> None:
    """Live-E2E regression: a valid sales token whose firebase uid maps to an
    active sales row must reach the transcript through the REAL ladder — the
    wrapper must not downgrade it to 401. Lead 51 is assigned to sales_id 1
    (the mapping of uid ``key-1``) and has no chat history, so the authorized
    outcome is the 200 empty transcript with private cache headers."""
    from api.infrastructure.dependencies import get_chat_history_repository

    matrix_app.dependency_overrides[get_chat_history_repository] = (
        lambda: FakeChatHistoryRepository()
    )
    client = TestClient(matrix_app)
    response = client.get(
        "/api/crm/leads/51/conversation",
        headers={
            **_bearer(local_rsa_jwk, firebase_uid="key-1", role="sales"),
            "X-Correlation-ID": "corr-sales-ok",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["messages"] == []
    assert body["session_id"] is None
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Vary"] == "Authorization"
    assert response.headers["X-Correlation-ID"] == "corr-sales-ok"


def test_transcript_real_auth_admin_token_reaches_empty_transcript(
    matrix_app: FastAPI, local_rsa_jwk: dict
) -> None:
    """Admin (no sales mapping by design) must also reach the 200 empty view."""
    from api.infrastructure.dependencies import get_chat_history_repository

    matrix_app.dependency_overrides[get_chat_history_repository] = (
        lambda: FakeChatHistoryRepository()
    )
    client = TestClient(matrix_app)
    response = client.get(
        "/api/crm/leads/51/conversation",
        headers=_bearer(local_rsa_jwk, firebase_uid="uid-admin", role="admin"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []


def test_transcript_real_auth_unmapped_sales_is_403_not_401(
    matrix_app: FastAPI, local_rsa_jwk: dict
) -> None:
    """Status-code discrimination: verified sales WITHOUT an active mapping is
    a 403 (authorization gap), never a 401 — 401 stays reserved for
    missing/invalid credentials."""
    client = TestClient(matrix_app)
    response = client.get(
        "/api/crm/leads/51/conversation",
        headers={
            **_bearer(local_rsa_jwk, firebase_uid="uid-sales-unmapped", role="sales"),
            "X-Correlation-ID": "corr-unmapped",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Authorization failed"}
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["X-Correlation-ID"] == "corr-unmapped"
