"""FCM notification wave: dual-mode device registration, server-side
call-started resolution, background lead push, pruning, and token caps.

Auth seams reuse tests/_auth_seams (offline RSA JWKS + fake sales mapping);
the anon mode reuses the runtime-generated ANON_IDENTITY_SECRET pattern from
tests/test_lead_bonus. No network, no live database.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import api.infrastructure.dependencies as dependency_injection
from api.application.ports.fcm_notifications import (
    IDENTITY_TYPE_ANON_CUSTOMER,
    IDENTITY_TYPE_FIREBASE,
    FcmTokenInvalidError,
)
from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.fcm_notification_service import FcmNotificationService
from api.infrastructure.adapters import postgres_fcm_tokens
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import LeadRow, SalesRow, get_lead_repository
from api.infrastructure.ports.realtime_mirror import get_realtime_lead_mirror
from api.interfaces.api import notifications
from api.interfaces.api.main import create_app
from tests._auth_seams import base_claims, mint_id_token, sales_bearer_headers
from tests.test_lead_bonus import InMemoryQuotaRecordStore
from tests.test_sales_api import FakeLeadRepository

VALID_TOKEN = "fcm-token-" + "x" * 40

# ----------------------------------------------------------------------------- fakes


class FakeFcmTokenRepository:
    """In-memory twin of the identity-keyed token store."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], bool] = {}
        self.pruned: list[str] = []

    def seed(self, identity_type: str, identity_key: str, *tokens: str) -> None:
        for token in tokens:
            self.rows[(identity_type, identity_key, token)] = True

    async def register_fcm_token(
        self, *, identity_type: str, identity_key: str, token: str, platform: str
    ) -> None:
        self.rows[(identity_type, identity_key, token)] = True

    async def remove_fcm_token(
        self, *, identity_type: str, identity_key: str, token: str
    ) -> None:
        if (identity_type, identity_key, token) in self.rows:
            self.rows[(identity_type, identity_key, token)] = False

    async def list_fcm_tokens(self, *, identity_type: str, identity_key: str) -> list[str]:
        return [
            token
            for (itype, ikey, token), enabled in self.rows.items()
            if enabled and itype == identity_type and ikey == identity_key
        ]

    async def list_fcm_tokens_for_sales(self, *, sales_id: int) -> list[str]:
        return []

    async def prune_token(self, *, token: str) -> None:
        self.pruned.append(token)
        for key in list(self.rows):
            if key[2] == token:
                self.rows[key] = False


class FakeFcmSender:
    def __init__(self, *, invalid_tokens: tuple[str, ...] = (), boom: bool = False) -> None:
        self.sent: list[dict] = []
        self.invalid_tokens = invalid_tokens
        self.boom = boom

    async def send(
        self, *, token: str, title: str | None, body: str | None, data: dict[str, str]
    ) -> None:
        if token in self.invalid_tokens:
            raise FcmTokenInvalidError("UNREGISTERED")
        if self.boom:
            raise RuntimeError("simulated FCM outage")
        self.sent.append(
            {"token": token, "title": title, "body": body, "data": dict(data)}
        )


class FakeAuditStore:
    def __init__(self) -> None:
        self.entries: list = []

    async def record_entry(self, entry) -> None:
        self.entries.append(entry)


class RecordingLeadRepository(FakeLeadRepository):
    """Captures create_lead kwargs so identity persistence is assertable."""

    def __init__(self) -> None:
        super().__init__()
        self.create_lead_calls: list[dict] = []
        self.events: list[str] = []

    async def create_lead(self, **kwargs) -> LeadRow:
        self.events.append("create_lead")
        self.create_lead_calls.append(dict(kwargs))
        lead = await super().create_lead(**kwargs)
        stored = replace(lead, customer_identity=kwargs.get("customer_identity"))
        self.leads[lead.id] = stored
        return stored


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def generated_anon_identity_secret(monkeypatch: pytest.MonkeyPatch):
    """Isolate anon token signing behind a runtime-generated secret."""
    secret = f"test-only-anon-secret-{uuid.uuid4().hex}"
    monkeypatch.setenv("ANON_IDENTITY_SECRET", secret)
    get_settings.cache_clear()
    yield secret
    get_settings.cache_clear()


def mint_anon_token() -> tuple[str, str]:
    service = AnonymousIdentityService(get_settings().anon_identity_secret)
    token = service.mint_token()
    claims = service.verify_token(token)
    assert claims is not None
    return token, claims.subject


def make_client(
    *,
    repo: RecordingLeadRepository | None = None,
    tokens: FakeFcmTokenRepository | None = None,
    sender: FakeFcmSender | None = None,
    audit: FakeAuditStore | None = None,
) -> tuple[TestClient, RecordingLeadRepository, FakeFcmTokenRepository, FakeFcmSender]:
    app = create_app()
    resolved_repo = repo if repo is not None else RecordingLeadRepository()
    resolved_tokens = tokens if tokens is not None else FakeFcmTokenRepository()
    resolved_sender = sender if sender is not None else FakeFcmSender()
    service = FcmNotificationService(resolved_tokens, resolved_sender)
    app.dependency_overrides[get_lead_repository] = lambda: resolved_repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: _NoopMirror()
    app.dependency_overrides[notifications.get_fcm_notification_service] = lambda: service
    from api.interfaces.api.lead import get_quota_storage

    app.dependency_overrides[get_quota_storage] = lambda: InMemoryQuotaRecordStore()
    return app_client(app), resolved_repo, resolved_tokens, resolved_sender


def app_client(app) -> TestClient:
    return TestClient(app)


class _NoopMirror:
    async def upsert_lead_mirror(self, *, document_id: str, document: object) -> None:
        return None

    async def remove_lead_mirror(self, document_id: str) -> None:
        return None


@pytest.fixture()
def patched_seams(monkeypatch: pytest.MonkeyPatch):
    """Point the direct-call seams (token store, audit store, push service) at fakes."""
    holder: dict[str, object] = {}

    def install(
        tokens: FakeFcmTokenRepository,
        audit: FakeAuditStore,
        service: FcmNotificationService,
    ) -> None:
        holder["tokens"] = tokens
        monkeypatch.setattr(notifications, "get_fcm_tokens", lambda: tokens)
        monkeypatch.setattr(notifications, "get_staff_audit_store", lambda: audit)
        monkeypatch.setattr(
            dependency_injection, "get_fcm_notification_service", lambda: service
        )

    return install


def sales_lead(lead_id: int = 1, *, assigned_sales_id: int | None = 777, **overrides) -> LeadRow:
    now = datetime.now(timezone.utc)
    fields: dict = {
        "id": lead_id,
        "session_id": None,
        "project_key": "camellia",
        "device_id": None,
        "name": "Khách Test",
        "phone": "0905123456",
        "consent": True,
        "note": None,
        "budget_vnd": None,
        "created_at": now,
        "status": "assigned",
        "assigned_sales_id": assigned_sales_id,
        "lock_expires_at": None,
        "escal_count": 0,
        "last_action_at": None,
        "closed_at": None,
        "customer_identity": "anon-subject-1",
    }
    fields.update(overrides)
    return LeadRow(**fields)


def sales_bearer(local_rsa_jwk: dict, uid: str = "uid-sales-mapped") -> dict[str, str]:
    return sales_bearer_headers(local_rsa_jwk, uid)


# ------------------------------------------------------------------- device-token tests


def test_register_device_token_with_firebase_bearer(
    offline_auth_seams, local_rsa_jwk, patched_seams  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    patched_seams(tokens, FakeAuditStore(), FcmNotificationService(tokens, FakeFcmSender()))
    client, _, _, _ = make_client(tokens=tokens)
    response = client.post(
        "/api/notifications/device-token",
        json={"token": VALID_TOKEN},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 204
    assert tokens.rows.get((IDENTITY_TYPE_FIREBASE, "uid-sales-mapped", VALID_TOKEN)) is True


def test_register_device_token_with_anon_header_and_body(
    patched_seams,
) -> None:
    tokens = FakeFcmTokenRepository()
    patched_seams(tokens, FakeAuditStore(), FcmNotificationService(tokens, FakeFcmSender()))
    client, _, _, _ = make_client(tokens=tokens)
    header_token, header_subject = mint_anon_token()
    body_token, body_subject = mint_anon_token()
    via_header = client.post(
        "/api/notifications/device-token",
        json={"token": VALID_TOKEN},
        headers={"X-Anon-Token": header_token},
    )
    assert via_header.status_code == 204
    assert tokens.rows.get((IDENTITY_TYPE_ANON_CUSTOMER, header_subject, VALID_TOKEN)) is True
    via_body = client.post(
        "/api/notifications/device-token",
        json={"token": VALID_TOKEN, "anon_token": body_token},
    )
    assert via_body.status_code == 204
    assert tokens.rows.get((IDENTITY_TYPE_ANON_CUSTOMER, body_subject, VALID_TOKEN)) is True


def test_register_device_token_rejects_invalid_anon_and_anonymous_caller(
    patched_seams,
) -> None:
    tokens = FakeFcmTokenRepository()
    patched_seams(tokens, FakeAuditStore(), FcmNotificationService(tokens, FakeFcmSender()))
    client, _, _, _ = make_client(tokens=tokens)
    bad = client.post(
        "/api/notifications/device-token",
        json={"token": VALID_TOKEN, "anon_token": "not-a-signed-token"},
    )
    assert bad.status_code == 401
    none = client.post("/api/notifications/device-token", json={"token": VALID_TOKEN})
    assert none.status_code == 401
    assert tokens.rows == {}


def test_delete_device_token_enforces_ownership_across_modes(
    offline_auth_seams, local_rsa_jwk, patched_seams  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    anon_token, anon_subject = mint_anon_token()
    tokens.seed(IDENTITY_TYPE_FIREBASE, "uid-sales-mapped", VALID_TOKEN)
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, anon_subject, VALID_TOKEN)
    patched_seams(tokens, FakeAuditStore(), FcmNotificationService(tokens, FakeFcmSender()))
    client, _, _, _ = make_client(tokens=tokens)
    # The sales user may only disable their OWN registration of the token.
    deleted = client.request(
        "DELETE",
        "/api/notifications/device-token",
        json={"token": VALID_TOKEN},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert deleted.status_code == 204
    assert tokens.rows[(IDENTITY_TYPE_FIREBASE, "uid-sales-mapped", VALID_TOKEN)] is False
    assert tokens.rows[(IDENTITY_TYPE_ANON_CUSTOMER, anon_subject, VALID_TOKEN)] is True
    # The anon customer removes their own; the other stays disabled.
    deleted_anon = client.request(
        "DELETE",
        "/api/notifications/device-token",
        json={"token": VALID_TOKEN},
        headers={"X-Anon-Token": anon_token},
    )
    assert deleted_anon.status_code == 204
    assert tokens.rows[(IDENTITY_TYPE_ANON_CUSTOMER, anon_subject, VALID_TOKEN)] is False


# -------------------------------------------------------------------- call-started tests


def _call_started_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lead: LeadRow,
    tokens: FakeFcmTokenRepository,
    sender: FakeFcmSender,
) -> tuple[TestClient, FakeAuditStore]:
    repo = RecordingLeadRepository()
    repo.sales.append(
        SalesRow(
            id=777,
            access_key="key-777",
            full_name="Tran Huy",
            role="sales",
            phone=None,
            is_active=True,
            priority=5,
            last_seen_at=None,
            firebase_uid="uid-sales-mapped",
        )
    )
    repo.leads[lead.id] = lead
    audit = FakeAuditStore()
    monkeypatch.setattr(notifications, "get_staff_audit_store", lambda: audit)
    # The background once-dispatch resolves its service through the lazy
    # dependency seam, not the route Depends, so patch that seam too.
    monkeypatch.setattr(
        dependency_injection,
        "get_fcm_notification_service",
        lambda: FcmNotificationService(tokens, sender),
    )
    client, _, _, _ = make_client(repo=repo, tokens=tokens, sender=sender)
    return client, audit


def test_call_started_resolves_recipient_server_side(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "anon-subject-1", "tok-a", "tok-b")
    sender = FakeFcmSender()
    client, audit = _call_started_setup(
        monkeypatch,
        lead=sales_lead(status="called"),
        tokens=tokens,
        sender=sender,
    )
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["dispatched_tokens"] == 2
    assert body["customer_has_device"] is True
    assert len(sender.sent) == 2
    payload = sender.sent[0]
    assert "Tran Huy" in payload["body"]
    assert payload["data"]["type"] == "sales_call_started"
    assert payload["data"]["url"] == "/project/camellia"
    # PII rule: the raw phone never rides in the notification payload.
    assert "0905123456" not in json.dumps(payload)
    assert audit.entries[0].action == "call_started"
    assert audit.entries[0].lead_id == 1


def test_call_started_reports_no_registered_device(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    client, _audit = _call_started_setup(
        monkeypatch, lead=sales_lead(), tokens=tokens, sender=FakeFcmSender()
    )
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "dispatched_tokens": 0,
        "customer_has_device": False,
    }


def test_call_started_reports_registered_devices_even_when_sends_fail(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    """dispatched_tokens counts REGISTERED devices resolved synchronously; the
    FCM sends run as a background task, so an outage cannot zero the count."""
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "anon-subject-1", "tok-a", "tok-b")
    sender = FakeFcmSender(boom=True)
    client, _audit = _call_started_setup(
        monkeypatch, lead=sales_lead(status="called"), tokens=tokens, sender=sender
    )
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "dispatched_tokens": 2,
        "customer_has_device": True,
    }
    # Background dispatch still attempted both sends (TestClient runs the task
    # before returning); the service swallowed the failures.
    assert len(sender.sent) == 0


def test_call_started_rejects_caller_supplied_customer_uid(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    client, _audit = _call_started_setup(
        monkeypatch, lead=sales_lead(), tokens=tokens, sender=FakeFcmSender()
    )
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1, "customer_firebase_uid": "attacker-chosen-uid"},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 422
    assert tokens.rows == {}


def test_call_started_sales_not_assigned_gets_403(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    client, audit = _call_started_setup(
        monkeypatch,
        lead=sales_lead(assigned_sales_id=999),
        tokens=tokens,
        sender=FakeFcmSender(),
    )
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 403
    assert audit.entries == []


def test_call_started_admin_bypasses_ownership(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "anon-subject-1", "tok-a")
    sender = FakeFcmSender()
    client, _audit = _call_started_setup(
        monkeypatch,
        lead=sales_lead(assigned_sales_id=999),
        tokens=tokens,
        sender=sender,
    )
    admin_token = mint_id_token(local_rsa_jwk, base_claims("uid-admin", "admin"))
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 202
    assert response.json()["dispatched_tokens"] == 1


def test_call_started_without_token_is_401(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    client, _audit = _call_started_setup(
        monkeypatch, lead=sales_lead(), tokens=tokens, sender=FakeFcmSender()
    )
    response = client.post("/api/notifications/call-started", json={"lead_id": 1})
    assert response.status_code == 401
    # Authentication fails before any audit entry or stamp work happens.
    assert _audit.entries == []


def test_call_started_duplicate_request_never_resends(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    """Exactly-once: the first accepted call-started claims the CAS stamp; a
    repeated POST (double click, retry storm) resolves the same tokens for the
    response but the background dispatch becomes a silent no-op."""
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "anon-subject-1", "tok-a", "tok-b")
    sender = FakeFcmSender()
    client, _audit = _call_started_setup(
        monkeypatch,
        lead=sales_lead(status="called"),
        tokens=tokens,
        sender=sender,
    )
    first = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert first.status_code == 202
    assert len(sender.sent) == 2
    second = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    # The response stays truthful (devices still registered)...
    assert second.status_code == 202
    assert second.json() == {
        "accepted": True,
        "dispatched_tokens": 2,
        "customer_has_device": True,
    }
    # ...but no duplicate push left the process.
    assert len(sender.sent) == 2


def test_call_started_send_failure_claims_stamp_without_retry(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    """At-most-once semantics: an FCM outage consumes the once-guard (the stamp
    stays claimed), so a later recovery + retry cannot double-notify the
    customer. Recovery requires a new event transition, not a replay."""
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "anon-subject-1", "tok-a")
    sender = FakeFcmSender(boom=True)
    client, _audit = _call_started_setup(
        monkeypatch,
        lead=sales_lead(status="called"),
        tokens=tokens,
        sender=sender,
    )
    first = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert first.status_code == 202
    assert first.json()["dispatched_tokens"] == 1
    assert sender.sent == []
    # Outage over — but the stamp was already claimed by the failed dispatch.
    sender.boom = False
    retry = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert retry.status_code == 202
    assert sender.sent == []


def test_call_started_legacy_row_without_identity_needs_no_stamp(
    offline_auth_seams, local_rsa_jwk, monkeypatch  # noqa: F401
) -> None:
    tokens = FakeFcmTokenRepository()
    client, _audit = _call_started_setup(
        monkeypatch,
        lead=sales_lead(customer_identity=None),
        tokens=tokens,
        sender=FakeFcmSender(),
    )
    response = client.post(
        "/api/notifications/call-started",
        json={"lead_id": 1},
        headers=sales_bearer(local_rsa_jwk),
    )
    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "dispatched_tokens": 0,
        "customer_has_device": False,
    }


@pytest.mark.asyncio
async def test_stamp_call_started_allows_single_winner() -> None:
    """Concurrent CAS: two racing dispatches stamp the same lead — exactly one
    wins; the loser must not send (fake twin is await-free, so gather() forces
    the interleaving deterministically)."""
    repo = RecordingLeadRepository()
    repo.leads[1] = sales_lead(status="called")
    outcomes = await asyncio.gather(
        repo.stamp_call_started(1),
        repo.stamp_call_started(1),
    )
    assert sorted(outcomes) == [False, True]
    assert repo.call_notified == {1}


def test_stamp_call_started_requires_called_status() -> None:
    """Wrong-status guard: the tel-link surface fired before the CRM PATCH has
    nothing to claim — the PATCH transition owns the notification."""
    repo = RecordingLeadRepository()
    repo.leads[1] = sales_lead(status="assigned")
    assert asyncio.run(repo.stamp_call_started(1)) is False
    assert repo.call_notified == set()


# ------------------------------------------------------------------ lead-created hook tests


def test_lead_created_push_runs_post_commit_and_survives_failure(
    patched_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = FakeFcmTokenRepository()
    sender = FakeFcmSender(boom=True)  # every send fails: the lead must still 201
    service = FcmNotificationService(tokens, sender)
    patched_seams(tokens, FakeAuditStore(), service)
    repo = RecordingLeadRepository()
    client, resolved_repo, _tokens, _sender = make_client(
        repo=repo, tokens=tokens, sender=sender
    )
    anon_token, anon_subject = mint_anon_token()
    response = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "phone": "0905000123",
            "consent": True,
            "name": "Khách A",
            "anon_token": anon_token,
        },
    )
    assert response.status_code == 201
    # Identity persisted at creation from the VERIFIED anon token (never raw phone).
    assert resolved_repo.create_lead_calls[0]["customer_identity"] == anon_subject
    # Background dispatch ran after the response: notify_sales attempted, and
    # its internal send failure was swallowed (service._send_one never raises).
    assert resolved_repo.events[0] == "create_lead"


def test_lead_created_push_payload_carries_sales_url_and_no_phone(
    patched_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SalesTokenRepo(FakeFcmTokenRepository):
        async def list_fcm_tokens_for_sales(self, *, sales_id: int) -> list[str]:
            return ["sales-token"]

    push_sender = FakeFcmSender()
    push_service = FcmNotificationService(_SalesTokenRepo(), push_sender)
    # The background task resolves the service through the DI module seam.
    monkeypatch.setattr(
        dependency_injection, "get_fcm_notification_service", lambda: push_service
    )
    tokens = FakeFcmTokenRepository()
    patched_seams(tokens, FakeAuditStore(), push_service)
    client, repo, _tokens, _sender = make_client(repo=RecordingLeadRepository())
    response = client.post(
        "/api/lead",
        json={"project_key": "camellia", "phone": "0905000456", "consent": True, "name": "B"},
    )
    assert response.status_code == 201
    assert repo.leads[1].assigned_sales_id is not None  # LRU assigned a fake sales
    # TestClient executes background tasks before returning, so the push has
    # already been dispatched against the sales token list.
    assert push_sender.sent[0]["data"]["url"] == "/sales/leads"
    assert push_sender.sent[0]["data"]["type"] == "new_lead"
    assert "0905000456" not in json.dumps(push_sender.sent[0])


# --------------------------------------------------------------------- service + adapter


def test_service_prunes_dead_tokens_and_reports_outcomes() -> None:
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "subj", "dead-token", "live-token")
    sender = FakeFcmSender(invalid_tokens=("dead-token",))
    service = FcmNotificationService(tokens, sender)
    report = asyncio.run(
        service.notify_identity(
            identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
            identity_key="subj",
            title="t",
            body="b",
            data={"url": "/"},
        )
    )
    assert report.tokens_found == 2
    assert report.dispatched == 1
    assert report.pruned == 1
    assert tokens.pruned == ["dead-token"]
    assert tokens.rows[(IDENTITY_TYPE_ANON_CUSTOMER, "subj", "dead-token")] is False


def test_service_bounds_total_dispatch_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.application.services.fcm_notification_service as fcm_service

    monkeypatch.setattr(fcm_service, "FCM_TOTAL_BUDGET_SECONDS", 0.05)

    class _SlowSender:
        async def send(self, *, token: str, title: str | None, body: str | None, data: dict) -> None:
            await asyncio.sleep(1.0)

    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "subj", "t1", "t2")
    service = FcmNotificationService(tokens, _SlowSender())
    report = asyncio.run(
        service.notify_identity(
            identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
            identity_key="subj",
            title="t",
            body="b",
            data={},
        )
    )
    assert report.dispatched == 0
    assert report.failed == 2


class _RecordingConn:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def execute(self, sql: str, *args) -> None:
        self._calls.append((sql, args))

    async def fetch(self, sql: str, *args) -> list:
        self._calls.append((sql, args))
        return []

    async def fetchval(self, sql: str, *args):
        self._calls.append((sql, args))
        return 987654

    def transaction(self):
        calls = self._calls

        class _TxCtx:
            async def __aenter__(self):
                calls.append(("BEGIN", ()))
                return self

            async def __aexit__(self, *exc) -> bool:
                calls.append(("COMMIT" if exc[0] is None else "ROLLBACK", ()))
                return False

        return _TxCtx()


class _RecordingPool:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self) -> _RecordingConn:
                return _RecordingConn(pool._calls)

            async def __aexit__(self, *exc) -> bool:
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_adapter_caps_tokens_per_identity_and_prunes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []

    async def _get_pool() -> _RecordingPool:
        # Adapter awaits get_lead_pool(); return a fresh recorder sharing `calls`.
        return _RecordingPool(calls)

    monkeypatch.setattr(postgres_fcm_tokens, "get_lead_pool", _get_pool)
    repo = postgres_fcm_tokens.PostgresFcmTokenRepository()
    await repo.register_fcm_token(
        identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
        identity_key="subj",
        token=VALID_TOKEN,
        platform="web",
    )
    # One transaction: BEGIN -> identity lock -> upsert -> eviction -> COMMIT.
    assert calls[0] == ("BEGIN", ())
    lock_sql, lock_args = calls[1]
    assert "pg_advisory_xact_lock" in lock_sql and "hashtextextended" in lock_sql
    assert lock_args == (f"fcm-register:{IDENTITY_TYPE_ANON_CUSTOMER}:subj",)
    upsert_sql, upsert_args = calls[2]
    assert "ON CONFLICT (identity_type, identity_key, token)" in upsert_sql
    assert "RETURNING id" in upsert_sql
    assert upsert_args == (IDENTITY_TYPE_ANON_CUSTOMER, "subj", None, VALID_TOKEN, "web")
    eviction_sql, eviction_args = calls[3]
    assert "NOT IN" in eviction_sql and "ORDER BY last_seen_at DESC" in eviction_sql
    # The freshly registered row is pinned out of eviction by its returned id.
    assert "id <> $4" in eviction_sql
    assert eviction_args == (
        IDENTITY_TYPE_ANON_CUSTOMER,
        "subj",
        postgres_fcm_tokens.MAX_TOKENS_PER_IDENTITY,
        987654,
    )
    assert calls[4] == ("COMMIT", ())
    calls.clear()
    await repo.prune_token(token=VALID_TOKEN)
    prune_sql, prune_args = calls[0]
    assert "SET enabled = false" in prune_sql and "WHERE token = $1" in prune_sql
    assert prune_args == (VALID_TOKEN,)


def test_adapter_register_serializes_per_identity_and_keeps_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrency guard: same-token re-register upserts (no duplicate row) and
    the eviction never removes the row the transaction just wrote."""
    calls: list = []

    async def _get_pool() -> _RecordingPool:
        return _RecordingPool(calls)

    monkeypatch.setattr(postgres_fcm_tokens, "get_lead_pool", _get_pool)
    repo = postgres_fcm_tokens.PostgresFcmTokenRepository()
    # Two registrations of the SAME token (retry storm) must both take the
    # identity-scoped lock and both pin the fresh id in their eviction.
    for _ in range(2):
        asyncio.run(
            repo.register_fcm_token(
                identity_type=IDENTITY_TYPE_FIREBASE,
                identity_key="uid-1",
                token=VALID_TOKEN,
                platform="web",
            )
        )
    lock_calls = [c for c in calls if "pg_advisory_xact_lock" in c[0]]
    assert len(lock_calls) == 2
    assert all(c[1] == ("fcm-register:firebase_uid:uid-1",) for c in lock_calls)
    upserts = [c for c in calls if "ON CONFLICT" in c[0]]
    assert len(upserts) == 2
    # Firebase rows keep the legacy firebase_uid column in sync.
    assert upserts[0][1] == (IDENTITY_TYPE_FIREBASE, "uid-1", "uid-1", VALID_TOKEN, "web")
    evictions = [c for c in calls if c[0].lstrip().startswith("DELETE")]
    assert len(evictions) == 2
    assert all("id <> $4" in sql for sql, _ in evictions)
    assert all(args[3] == 987654 for _, args in evictions)
    # Every statement ran inside a single transaction per call.
    assert [c[0] for c in calls if c[1] == ()] == ["BEGIN", "COMMIT", "BEGIN", "COMMIT"]


def test_send_test_resolves_callers_own_tokens_and_is_data_only() -> None:
    """send_test must only reach the caller's own identity tokens and dispatch a
    data-only (title/body None) message so the SW onBackgroundMessage path is
    deterministically exercised for background-delivery verification."""
    tokens = FakeFcmTokenRepository()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "owner-subj", "tok-owner-1", "tok-owner-2")
    # A different identity must NOT be reached by this caller's test ping.
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "other-subj", "tok-other-1")
    sender = FakeFcmSender()
    service = FcmNotificationService(tokens, sender)
    report = asyncio.run(
        service.send_test(
            identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
            identity_key="owner-subj",
            data={},  # the route injects type/sent_at; service must pass through as-is
        )
    )
    assert report is not None
    assert report.tokens_found == 2
    assert report.dispatched == 2
    assert len(sender.sent) == 2
    for payload in sender.sent:
        assert payload["title"] is None
        assert payload["body"] is None
        # The route layer is responsible for injecting the fcm_test marker; the
        # service must pass the caller's data through verbatim (no type added).
        assert payload["data"] == {}
        assert payload["token"] in {"tok-owner-1", "tok-owner-2"}
    # No leakage to the other identity's device.
    assert all(p["token"] != "tok-other-1" for p in sender.sent)


def test_send_test_returns_none_when_no_enabled_tokens() -> None:
    tokens = FakeFcmTokenRepository()
    sender = FakeFcmSender()
    service = FcmNotificationService(tokens, sender)
    report = asyncio.run(
        service.send_test(
            identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
            identity_key="no-device-subj",
            data={},
        )
    )
    assert report is None
    assert sender.sent == []


def test_adapter_register_rolls_back_eviction_when_upsert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []

    class _FailingConn(_RecordingConn):
        async def fetchval(self, sql: str, *args):
            self._calls.append((sql, args))
            raise RuntimeError("simulated insert failure")

    async def _get_pool():
        class _Pool:
            def acquire(self):
                class _Ctx:
                    async def __aenter__(self) -> _RecordingConn:
                        return _FailingConn(calls)

                    async def __aexit__(self, *exc) -> bool:
                        return False

                return _Ctx()

        return _Pool()

    monkeypatch.setattr(postgres_fcm_tokens, "get_lead_pool", _get_pool)
    repo = postgres_fcm_tokens.PostgresFcmTokenRepository()
    with pytest.raises(RuntimeError):
        asyncio.run(
            repo.register_fcm_token(
                identity_type=IDENTITY_TYPE_ANON_CUSTOMER,
                identity_key="subj",
                token=VALID_TOKEN,
                platform="web",
            )
        )
    # No eviction statement ran, and the transaction rolled back — a failed
    # upsert can never leave a half-applied cap deletion.
    assert not any(c[0].lstrip().startswith("DELETE") for c in calls)
    assert calls[-1] == ("ROLLBACK", ())


# --------------------------------------------------------------------- /test endpoint


def _make_test_client(
    *,
    tokens: FakeFcmTokenRepository,
    sender: FakeFcmSender,
    rate_limit_store=None,
) -> tuple[TestClient, FakeFcmTokenRepository, FakeFcmSender]:
    """Assemble an app with the FCM service pointed at fakes.

    The rate-limit store is NOT wired here: the route reads get_rate_limit_port
    and get_client_ip_address as module-level globals (bound at import), so a
    dependency_overrides on the anon_identity seam has no effect. Tests that need
    deterministic rate-limit behaviour must monkeypatch those globals directly
    (see test_test_endpoint_enforces_rate_limit).
    """
    service = FcmNotificationService(tokens, sender)
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: RecordingLeadRepository()
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: _NoopMirror()
    app.dependency_overrides[notifications.get_fcm_notification_service] = lambda: service
    app.dependency_overrides[notifications.get_fcm_tokens] = lambda: tokens
    return TestClient(app), tokens, sender


class _FixedAllowanceRateLimitStore:
    """Rate-limit store the test fully controls: returns a set value once."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.checked: list[tuple] = []

    async def check_rate_limit_allowed(
        self, ip_address: str, kind: str, limit: int, window_seconds: int
    ) -> bool:
        self.checked.append((ip_address, kind, limit, window_seconds))
        return self.allowed


def test_test_endpoint_happy_path_with_anon_identity(
    patched_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/notifications/test succeeds for an anon customer and dispatches a
    data-only message to only that caller's own registered device(s)."""
    tokens = FakeFcmTokenRepository()
    anon_token, anon_subject = mint_anon_token()
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, anon_subject, "tok-anon-1")
    # Another customer's device must never be targeted by this caller's test.
    tokens.seed(IDENTITY_TYPE_ANON_CUSTOMER, "other-subj", "tok-other-1")
    sender = FakeFcmSender()
    client, _, _ = _make_test_client(tokens=tokens, sender=sender)

    response = client.post(
        "/api/notifications/test",
        headers={"X-Anon-Token": anon_token},
    )
    assert response.status_code == 202
    body = response.json()
    assert body == {"dispatched_tokens": 1, "customer_has_device": True}
    # TestClient drains background tasks inline; exactly the caller's device got
    # a data-only push (title/body None, type fcm_test).
    assert len(sender.sent) == 1
    payload = sender.sent[0]
    assert payload["token"] == "tok-anon-1"
    assert payload["title"] is None
    assert payload["body"] is None
    assert payload["data"]["type"] == "fcm_test"
    assert "sent_at" in payload["data"]


def test_test_endpoint_reports_no_device_without_404(
    patched_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unregistered identity returns 202 + dispatched_tokens=0 (NOT 404), per
    the call-started precedent: an empty device set is an expected state, not an
    error."""
    tokens = FakeFcmTokenRepository()
    anon_token, _anon_subject = mint_anon_token()
    sender = FakeFcmSender()
    client, _, _ = _make_test_client(tokens=tokens, sender=sender)

    response = client.post(
        "/api/notifications/test",
        headers={"X-Anon-Token": anon_token},
    )
    assert response.status_code == 202
    assert response.json() == {"dispatched_tokens": 0, "customer_has_device": False}
    assert sender.sent == []


def test_test_endpoint_requires_valid_identity(patched_seams, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = FakeFcmTokenRepository()
    sender = FakeFcmSender()
    client, _, _ = _make_test_client(tokens=tokens, sender=sender)
    # No bearer, no anon token -> 401 (same auth gate as device registration).
    bad = client.post("/api/notifications/test", json={})
    assert bad.status_code == 401
    assert sender.sent == []


def test_test_endpoint_enforces_rate_limit(
    patched_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rate-limit brake must still fire on /test — a blocked caller gets 429
    before any dispatch work happens."""
    tokens = FakeFcmTokenRepository()
    anon_token, _anon_subject = mint_anon_token()
    sender = FakeFcmSender()
    store = _FixedAllowanceRateLimitStore(allowed=False)
    client, _, _ = _make_test_client(tokens=tokens, sender=sender)

    # The route reads get_rate_limit_port / get_client_ip_address as module globals
    # (bound at import), so a dependency_overrides on the anon_identity seam has no
    # effect — patch the live globals on every module that references them.
    import api.application.services.anon_identity as anon_identity
    import api.interfaces.api.anon_routes as anon_routes
    import api.interfaces.api.notifications as notifications_module

    original_port = anon_identity.get_rate_limit_port()
    original_get_ip = anon_routes.get_client_ip_address
    anon_identity.set_rate_limit_port(store)
    anon_routes.get_client_ip_address = lambda request, **kw: "127.0.0.1"
    notifications_module.get_client_ip_address = anon_routes.get_client_ip_address
    try:
        response = client.post(
            "/api/notifications/test",
            headers={"X-Anon-Token": anon_token},
        )
        assert response.status_code == 429
        assert sender.sent == []
        # The brake keyed on the shared fcm-register kind.
        assert store.checked[0][1] == notifications.RATE_LIMIT_KIND_FCM_REGISTER
    finally:
        anon_identity.set_rate_limit_port(original_port)
        anon_routes.get_client_ip_address = original_get_ip
        notifications_module.get_client_ip_address = original_get_ip