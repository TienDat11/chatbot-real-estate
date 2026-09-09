"""FR-33 / BE-CALL-NOTIFY-ONCE — exactly-once customer call-started notification.

Focus: the CRM PATCH assigned->called path claims the DB CAS stamp
(``leads.call_notified_at``) exactly once, converges with the tel-link
``/api/notifications/call-started`` surface (no duplicate push), never reaches
the sales recipient, and never leaks raw phone in the payload.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from api.interfaces.api import notifications as notifications_module
from tests.test_crm_api import (
    ASSIGNED_SALES_PRINCIPAL,
    CUSTOMER_PHONE,
    FakeCrmLeadRepository,
    OTHER_SALES_PRINCIPAL,
    make_crm_client,
    seed_lead,
)
from tests.test_lead_mirror import RecordingLeadMirror
from api.infrastructure import dependencies as dependency_injection


class RecordingCustomerFcmService:
    """Records customer token fan-out; notify_sales must never be invoked for
    a customer event, so its calls are recorded and asserted empty."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.sales_calls: list[dict] = []

    async def resolve_identity_tokens(self, **kwargs) -> list[str]:
        return ["tok-a", "tok-b"]

    async def dispatch_to_tokens(self, *, tokens, title, body, data) -> None:
        for token in tokens:
            self.sent.append({"token": token, "title": title, "body": body, "data": dict(data)})

    async def notify_sales(self, **kwargs) -> None:
        self.sales_calls.append(kwargs)


def _seed_called_candidate(repo: FakeCrmLeadRepository, lead_id: int = 40) -> None:
    seed_lead(repo, lead_id, status="assigned")
    repo.leads[lead_id] = replace(repo.leads[lead_id], customer_identity="anon-subject-1")


def _patch_customer_service(monkeypatch: pytest.MonkeyPatch, service) -> None:
    # The once-dispatch resolves its FCM service lazily through the DI module
    # (same seam the sales-facing push uses), so patch there — not on the app.
    monkeypatch.setattr(dependency_injection, "get_fcm_notification_service", lambda: service)


def test_status_patch_assigned_to_called_notifies_customer_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeCrmLeadRepository()
    _seed_called_candidate(repo, 40)
    service = RecordingCustomerFcmService()
    _patch_customer_service(monkeypatch, service)
    client = make_crm_client(repo, RecordingLeadMirror())

    response = client.patch("/api/crm/leads/40/status", json={"status": "called"})

    assert response.status_code == 200
    assert response.json()["lead_status"] == "called"
    # Exactly one customer push (TestClient drains background tasks inline).
    assert len(service.sent) == 2
    payload = service.sent[0]
    assert payload["data"]["type"] == "sales_call_started"
    assert payload["data"]["lead_id"] == "40"
    assert payload["data"]["url"] == "/project/camellia"  # same-origin relative
    assert CUSTOMER_PHONE not in json.dumps(payload)  # no PII rides in the payload
    # A customer event must never reach the sales recipient.
    assert service.sales_calls == []
    assert repo.call_notified == {40}


def test_status_patch_callback_to_called_does_not_notify_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the assigned->called transition owns the notification; re-marking a
    callback lead 'called' is workflow bookkeeping, not a new call event."""
    repo = FakeCrmLeadRepository()
    _seed_called_candidate(repo, 41)
    repo.leads[41] = replace(repo.leads[41], status="callback")
    service = RecordingCustomerFcmService()
    _patch_customer_service(monkeypatch, service)
    client = make_crm_client(repo, RecordingLeadMirror())

    response = client.patch("/api/crm/leads/41/status", json={"status": "called"})

    assert response.status_code == 200
    assert service.sent == []
    assert repo.call_notified == set()


def test_status_patch_repeat_called_is_not_a_second_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client retry storm: a lead already 'called' with the stamp claimed gets
    no second push even when the PATCH is replayed."""
    repo = FakeCrmLeadRepository()
    _seed_called_candidate(repo, 46)
    repo.leads[46] = replace(repo.leads[46], status="called")
    repo.call_notified.add(46)  # the first event already consumed the guard
    service = RecordingCustomerFcmService()
    _patch_customer_service(monkeypatch, service)
    client = make_crm_client(repo, RecordingLeadMirror())

    response = client.patch("/api/crm/leads/46/status", json={"status": "called"})

    assert response.status_code == 200
    assert service.sent == []
    assert repo.call_notified == {46}


def test_crm_patch_then_tel_link_converges_on_single_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-path convergence: after the CRM PATCH claimed the stamp, the
    tel-link /api/notifications/call-started surface must not duplicate."""
    repo = FakeCrmLeadRepository()
    _seed_called_candidate(repo, 42)
    service = RecordingCustomerFcmService()
    _patch_customer_service(monkeypatch, service)
    client = make_crm_client(repo, RecordingLeadMirror())
    client.app.dependency_overrides[notifications_module.get_fcm_notification_service] = (
        lambda: service
    )

    first = client.patch("/api/crm/leads/42/status", json={"status": "called"})
    assert first.status_code == 200
    assert len(service.sent) == 2

    tel_link = client.post("/api/notifications/call-started", json={"lead_id": 42})
    assert tel_link.status_code == 202
    # Presence stays truthful, but the stamp forces the second dispatch to be a
    # no-op: still exactly one push per lead.
    assert tel_link.json() == {
        "accepted": True,
        "dispatched_tokens": 2,
        "customer_has_device": True,
    }
    assert len(service.sent) == 2


def test_status_patch_wrong_owner_is_403_and_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeCrmLeadRepository()
    _seed_called_candidate(repo, 43)
    service = RecordingCustomerFcmService()
    _patch_customer_service(monkeypatch, service)
    client = make_crm_client(repo, RecordingLeadMirror(), OTHER_SALES_PRINCIPAL)

    response = client.patch("/api/crm/leads/43/status", json={"status": "called"})

    assert response.status_code == 403
    assert service.sent == []
    assert repo.call_notified == set()


def test_invalid_status_transition_is_stable_422_and_never_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeCrmLeadRepository()
    _seed_called_candidate(repo, 44)
    service = RecordingCustomerFcmService()
    _patch_customer_service(monkeypatch, service)
    client = make_crm_client(repo, RecordingLeadMirror(), ASSIGNED_SALES_PRINCIPAL)

    response = client.patch("/api/crm/leads/44/status", json={"status": "new"})

    assert response.status_code == 422
    assert service.sent == []
    assert repo.call_notified == set()
