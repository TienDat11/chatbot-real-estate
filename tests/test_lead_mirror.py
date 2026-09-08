"""Story 9.2: PG-to-Firestore lead dual-write mirror + reconciliation (offline).

The mirror is exercised through the real POST /api/lead handler with the DI
providers swapped for in-memory fakes — no network, no Firestore. Focus:
document field correctness (masked phone, consent split, project_key, numeric
lead_id), the ADR-0004 per-lead document key (same phone -> distinct
documents, legacy customer-keyed doc cleaned up), best-effort failure
isolation (HTTP 201 + row flagged 'failed'), and sweep convergence.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.interfaces.api.lead as lead_module
import api.interfaces.api.main as main_module
from api.application.services.lead_mirror_reconciliation import (
    sweep_stale_lead_mirrors_once,
)
from api.application.services.lead_mirror_service import (
    build_lead_mirror_document,
    compute_customer_id,
    compute_lead_document_id,
)
from api.infrastructure.ports.leads import LeadRow, get_lead_repository
from api.infrastructure.ports.realtime_mirror import (
    LeadMirrorDocument,
    get_realtime_lead_mirror,
)
from api.interfaces.api.main import _maybe_start_lead_mirror_reconciliation, create_app
from tests.test_sales_api import FakeLeadRepository, sales_row


class RecordingLeadMirror:
    """In-memory RealtimeLeadMirror double keyed by the ADR-0004 document id."""

    def __init__(self, *, fail_upserts: bool = False) -> None:
        self.documents: dict[str, LeadMirrorDocument] = {}
        self.removed_document_ids: list[str] = []
        self.upsert_calls = 0
        self.fail_upserts = fail_upserts

    async def upsert_lead_mirror(self, *, document_id: str, document: LeadMirrorDocument) -> None:
        self.upsert_calls += 1
        if self.fail_upserts:
            raise RuntimeError("mirror transport down")
        self.documents[document_id] = document

    async def remove_lead_mirror(self, document_id: str) -> None:
        self.removed_document_ids.append(document_id)
        self.documents.pop(document_id, None)
        return None

    async def health_check(self) -> bool:
        return not self.fail_upserts


def make_client(
    mirror: RecordingLeadMirror,
    *,
    phone_cooldown_seconds: int | None = None,
) -> tuple[TestClient, FakeLeadRepository]:
    """App whose lead persistence and mirror are both in-memory fakes."""
    repo = FakeLeadRepository()
    from api.infrastructure.config.config import get_settings

    object.__setattr__(get_settings(), "sales_legacy_key_auth_enabled", True)
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: mirror
    if phone_cooldown_seconds is not None:
        app.dependency_overrides[lead_module.get_phone_cooldown_seconds] = lambda: (
            phone_cooldown_seconds
        )
    return TestClient(app), repo


def submit_lead_payload(phone: str = "0905123456") -> dict:
    return {
        "project_key": "camellia",
        "session_id": "session-mirror-1",
        "name": "Anh Test",
        "phone": phone,
        "consent": True,
        "note": "Quan tâm căn 2PN",
    }


def test_submit_lead_mirrors_document_with_masked_phone_and_consent_split(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mirror = RecordingLeadMirror()
    client, repo = make_client(mirror)
    with caplog.at_level("WARNING", logger="api.lead_mirror_service"):
        response = client.post("/api/lead", json=submit_lead_payload())

    assert response.status_code == 201
    assert mirror.upsert_calls == 1
    assert sum(
        "legacy sales access_key fallback used" in record.message
        for record in caplog.records
    ) == 1
    document = mirror.documents[compute_lead_document_id(1)]
    # Raw phone must never reach the mirror — only the masked form.
    assert document.masked_phone == "0905***456"
    assert "0905123456" not in str(document)
    # ADR-0004: the document id is the per-lead HMAC; the phone HMAC survives
    # as a customer-scoped FIELD, and the numeric PG id rides along for the
    # FE integer-only CRM routes.
    assert document.customer_id == compute_customer_id("0905123456")
    assert document.lead_id == 1
    # Consent split: legacy single consent=true maps to service consent;
    # marketing consent is a separate opt-in and stays false.
    assert document.consent_service is True
    assert document.consent_marketing is False
    assert document.project_key == "camellia"
    assert document.lead_status == "assigned"
    assert document.display_name == "Anh Test"
    # sales.access_key doubles as the sales Firebase uid (story 8.3 mapping).
    assert document.assigned_sales_firebase_uid == "key-1"
    # The PG row is flagged converged once the mirror write succeeded.
    assert repo.leads[1].mirror_status == "done"


def test_submit_lead_mirror_carries_provisioned_firebase_uid_over_access_key() -> None:
    """Issue 4F: provisioned rows map a real Identity Toolkit uid; the mirror
    must carry THAT uid (the one realtime clients isolate by), never the
    legacy access key — otherwise the lead orphans from its owner."""
    mirror = RecordingLeadMirror()
    client, repo = make_client(mirror)
    repo.sales = [
        sales_row(1, priority=10, firebase_uid="uid-1"),
        sales_row(2, priority=5, firebase_uid="uid-2"),
    ]
    response = client.post("/api/lead", json=submit_lead_payload())

    assert response.status_code == 201
    document = mirror.documents[compute_lead_document_id(1)]
    assert document.lead_status == "assigned"
    assert document.assigned_sales_firebase_uid == "uid-1"


def test_no_answer_reassignment_updates_firestore_mirror() -> None:
    """Regression: a no-answer reassignment used to update PG only, leaving
    the Firestore mirror on the OLD sales forever."""
    mirror = RecordingLeadMirror()
    client, repo = make_client(mirror)
    repo.sales = [
        sales_row(1, priority=10, firebase_uid="uid-1"),
        sales_row(2, priority=5, firebase_uid="uid-2"),
    ]
    assert client.post("/api/lead", json=submit_lead_payload()).status_code == 201
    document_id = compute_lead_document_id(1)
    assert mirror.documents[document_id].assigned_sales_firebase_uid == "uid-1"

    action = client.post(
        "/api/sales/leads/1/action",
        json={"action": "no_answer"},
        headers={"X-Sales-Key": "key-1"},
    )
    assert action.status_code == 200

    # The reassignment pushed a fresh snapshot carrying the NEW owner.
    assert mirror.upsert_calls == 2
    updated_document = mirror.documents[document_id]
    assert updated_document.assigned_sales_firebase_uid == "uid-2"
    assert updated_document.lead_status == "assigned"
    assert repo.leads[1].assigned_sales_id == 2


def test_submit_without_active_sales_mirrors_expired_and_omits_sales_uid() -> None:
    """Deliberate convention pin: with zero active sales the lead expires and
    the mirror omits the sales field entirely (adapter drops None strings;
    clients normalize missing to null) instead of inventing a sentinel."""
    mirror = RecordingLeadMirror()
    client, repo = make_client(mirror)
    repo.sales = []
    response = client.post("/api/lead", json=submit_lead_payload())

    assert response.status_code == 201
    document = mirror.documents[compute_lead_document_id(1)]
    assert document.lead_status == "expired"
    assert document.assigned_sales_firebase_uid is None
    assert repo.leads[1].status == "expired"


def test_mirror_failure_marks_row_failed_without_losing_the_lead() -> None:
    mirror = RecordingLeadMirror(fail_upserts=True)
    client, repo = make_client(mirror)
    response = client.post("/api/lead", json=submit_lead_payload())

    # The mirror failure never surfaces in the customer-facing response.
    assert response.status_code == 201
    assert response.json()["lead_id"] == 1
    # The PG row survives intact — assignment untouched — but is flagged for retry.
    lead = repo.leads[1]
    assert lead.mirror_status == "failed"
    assert lead.phone == "0905123456"
    assert lead.assigned_sales_id == 1
    assert lead.status == "assigned"


def test_double_submit_same_phone_writes_distinct_per_lead_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0004 collision regression: the pre-fix mirror keyed documents by
    HMAC(phone), so a second lead of the same customer overwrote the first
    document's project/owner/status. The per-lead document id makes them
    distinct documents; each write also erases its legacy customer-keyed doc.
    """
    # The spec §9 phone-cooldown brake would 429 the second POST; this test
    # pins mirror identity, so bypass the lookup seam for this test only.
    async def no_phone_cooldown(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(lead_module, "find_recent_lead_within_phone_cooldown", no_phone_cooldown)
    mirror = RecordingLeadMirror()
    client, repo = make_client(mirror)
    first = client.post("/api/lead", json=submit_lead_payload())
    second = client.post("/api/lead", json=submit_lead_payload())

    assert first.status_code == 201 and second.status_code == 201
    assert mirror.upsert_calls == 2
    first_document_id = compute_lead_document_id(1)
    second_document_id = compute_lead_document_id(2)
    assert set(mirror.documents) == {first_document_id, second_document_id}
    # Both leads of one customer keep their own owner/status projection.
    assert mirror.documents[first_document_id].lead_status == "assigned"
    assert mirror.documents[second_document_id].lead_status == "assigned"
    assert mirror.documents[second_document_id].customer_id == compute_customer_id(
        "0905123456"
    )
    # Every successful write erased the pre-ADR-0004 customer-keyed document.
    assert mirror.removed_document_ids == [
        compute_customer_id("0905123456"),
        compute_customer_id("0905123456"),
    ]
    assert repo.leads[2].mirror_status == "done"


def test_build_document_honors_explicit_consent_split_columns() -> None:
    now = datetime.now()
    lead = LeadRow(
        id=7,
        session_id=None,
        project_key="soleil",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
        created_at=now,
        status="assigned",
        assigned_sales_id=2,
        lock_expires_at=None,
        escal_count=0,
        last_action_at=None,
        closed_at=None,
        consent_service=False,
        consent_marketing=True,
        consent_at=now,
    )
    document = build_lead_mirror_document(lead, sales=None)
    # The split columns win over the legacy flag when present.
    assert document.consent_service is False
    assert document.consent_marketing is True
    assert document.consent_recorded_at == now.isoformat()
    assert document.assigned_sales_firebase_uid is None
    assert document.project_key == "soleil"
    # ADR-0004: the numeric PG id travels as a field for the integer-only
    # CRM routes; the document id itself is derived from it by the writer.
    assert document.lead_id == 7
    assert document.customer_id == compute_customer_id("0905123456")


@pytest.mark.asyncio
async def test_reconciliation_sweep_retries_failed_rows_until_converged() -> None:
    failing_mirror = RecordingLeadMirror(fail_upserts=True)
    client, repo = make_client(failing_mirror)
    assert client.post("/api/lead", json=submit_lead_payload()).status_code == 201
    assert repo.leads[1].mirror_status == "failed"

    # Transport recovers: the sweep re-runs the same mirror service and the
    # row converges to 'done'.
    recovered_mirror = RecordingLeadMirror()
    outcome = await sweep_stale_lead_mirrors_once(
        repo=repo,
        mirror=recovered_mirror,
        stale_before=datetime.now() + timedelta(minutes=1),
    )
    assert outcome.retried == 1
    assert outcome.converged == 1
    assert repo.leads[1].mirror_status == "done"
    assert set(recovered_mirror.documents) == {compute_lead_document_id(1)}

    # Convergence: a second sweep finds nothing stale left to retry.
    idle_outcome = await sweep_stale_lead_mirrors_once(
        repo=repo,
        mirror=recovered_mirror,
        stale_before=datetime.now() + timedelta(minutes=1),
    )
    assert idle_outcome.retried == 0
    assert idle_outcome.converged == 0


@pytest.mark.asyncio
async def test_reconciliation_sweep_keeps_failing_rows_flagged_for_next_round() -> None:
    still_failing_mirror = RecordingLeadMirror(fail_upserts=True)
    client, repo = make_client(still_failing_mirror)
    assert client.post("/api/lead", json=submit_lead_payload()).status_code == 201

    outcome = await sweep_stale_lead_mirrors_once(
        repo=repo,
        mirror=still_failing_mirror,
        stale_before=datetime.now() + timedelta(minutes=1),
    )
    assert outcome.retried == 1
    assert outcome.converged == 0
    assert outcome.still_pending_or_failed == 1
    assert repo.leads[1].mirror_status == "failed"


def test_sweep_ignores_fresh_failed_rows_inside_staleness_window() -> None:
    failing_mirror = RecordingLeadMirror(fail_upserts=True)
    client, repo = make_client(failing_mirror)
    assert client.post("/api/lead", json=submit_lead_payload()).status_code == 201
    assert repo.leads[1].mirror_status == "failed"

    # The row is failed but younger than the cutoff — sweeping now must skip
    # it; it only becomes eligible once older than the staleness window.
    recovered_mirror = RecordingLeadMirror()
    outcome = asyncio.run(
        sweep_stale_lead_mirrors_once(
            repo=repo,
            mirror=recovered_mirror,
            stale_before=datetime.now() - timedelta(minutes=300),
        )
    )
    assert outcome.retried == 0
    assert recovered_mirror.upsert_calls == 0


def test_reconciliation_task_not_started_when_binding_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module,
        "get_cfg",
        lambda key, default=None: {"firebase_binding": "off"}.get(key, default),
    )
    assert _maybe_start_lead_mirror_reconciliation() is None


def test_reconciliation_task_not_started_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"firebase_binding": "firestore", "lead_mirror_reconciliation_enabled": False}
    monkeypatch.setattr(main_module, "get_cfg", lambda key, default=None: config.get(key, default))
    assert _maybe_start_lead_mirror_reconciliation() is None


@pytest.mark.asyncio
async def test_reconciliation_task_starts_under_firestore_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"firebase_binding": "firestore", "lead_mirror_reconciliation_enabled": True}
    monkeypatch.setattr(main_module, "get_cfg", lambda key, default=None: config.get(key, default))
    task = _maybe_start_lead_mirror_reconciliation()
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
