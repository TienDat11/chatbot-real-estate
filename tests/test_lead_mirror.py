"""Story 9.2: PG-to-Firestore lead dual-write mirror + reconciliation (offline).

The mirror is exercised through the real POST /api/lead handler with the DI
providers swapped for in-memory fakes — no network, no Firestore. Focus:
document field correctness (masked phone, consent split, project_key),
best-effort failure isolation (HTTP 201 + row flagged 'failed'), sweep
convergence, and customer_id idempotency on double submit.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import api.interfaces.api.main as main_module
from api.application.services.lead_mirror_reconciliation import (
    sweep_stale_lead_mirrors_once,
)
from api.application.services.lead_mirror_service import (
    build_lead_mirror_document,
    compute_customer_id,
)
from api.infrastructure.ports.leads import LeadRow, get_lead_repository
from api.infrastructure.ports.realtime_mirror import (
    LeadMirrorDocument,
    get_realtime_lead_mirror,
)
from api.interfaces.api.main import _maybe_start_lead_mirror_reconciliation, create_app
from tests.test_sales_api import FakeLeadRepository


class RecordingLeadMirror:
    """In-memory RealtimeLeadMirror double that records upserts by customer_id."""

    def __init__(self, *, fail_upserts: bool = False) -> None:
        self.documents: dict[str, LeadMirrorDocument] = {}
        self.upsert_calls = 0
        self.fail_upserts = fail_upserts

    async def upsert_lead_mirror(self, *, customer_id: str, document: LeadMirrorDocument) -> None:
        self.upsert_calls += 1
        if self.fail_upserts:
            raise RuntimeError("mirror transport down")
        self.documents[customer_id] = document

    async def remove_lead_mirror(self, customer_id: str) -> None:
        return None

    async def health_check(self) -> bool:
        return not self.fail_upserts


def make_client(mirror: RecordingLeadMirror) -> tuple[TestClient, FakeLeadRepository]:
    """App whose lead persistence and mirror are both in-memory fakes."""
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: mirror
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


def test_submit_lead_mirrors_document_with_masked_phone_and_consent_split() -> None:
    mirror = RecordingLeadMirror()
    client, repo = make_client(mirror)
    response = client.post("/api/lead", json=submit_lead_payload())

    assert response.status_code == 201
    assert mirror.upsert_calls == 1
    document = mirror.documents[compute_customer_id("0905123456")]
    # Raw phone must never reach the mirror — only the masked form.
    assert document.masked_phone == "0905***456"
    assert "0905123456" not in str(document)
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


def test_double_submit_same_phone_writes_single_mirror_document() -> None:
    mirror = RecordingLeadMirror()
    client, _ = make_client(mirror)
    first = client.post("/api/lead", json=submit_lead_payload())
    second = client.post("/api/lead", json=submit_lead_payload())

    assert first.status_code == 201 and second.status_code == 201
    # Same phone -> same HMAC customer_id -> one Firestore doc, overwritten.
    assert mirror.upsert_calls == 2
    assert len(mirror.documents) == 1
    assert set(mirror.documents) == {compute_customer_id("0905123456")}


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
    assert set(recovered_mirror.documents) == {compute_customer_id("0905123456")}

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
    monkeypatch.setattr(
        main_module, "get_cfg", lambda key, default=None: config.get(key, default)
    )
    assert _maybe_start_lead_mirror_reconciliation() is None


@pytest.mark.asyncio
async def test_reconciliation_task_starts_under_firestore_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"firebase_binding": "firestore", "lead_mirror_reconciliation_enabled": True}
    monkeypatch.setattr(
        main_module, "get_cfg", lambda key, default=None: config.get(key, default)
    )
    task = _maybe_start_lead_mirror_reconciliation()
    assert task is not None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
