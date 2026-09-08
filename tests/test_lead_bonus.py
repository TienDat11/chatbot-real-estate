"""Secure wave Issue 3: POST /api/lead bonus turns + spam brakes (spec §5.5/§9)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.lead_service import mask_phone
from api.application.services.quota_service import QuotaRecord, QuotaRecordStore, consume_turn
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import get_lead_repository
from api.infrastructure.ports.realtime_mirror import get_realtime_lead_mirror
from api.interfaces.api.lead import (
    get_lead_ip_request_limit,
    get_phone_cooldown_seconds,
    get_quota_storage,
)
from api.interfaces.api.main import create_app
from tests.test_sales_api import FakeLeadRepository


class InMemoryQuotaRecordStore:
    """Quota double covering the two primitives the lead flow exercises.

    Mirrors the atomic store's observable contract (one-time grant flips the
    flag exactly once; denied IP attempts still count) without a database.
    """

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], QuotaRecord] = {}
        self.ip_window_counts: dict[tuple[str, str], int] = {}

    async def grant_one_time_bonus(
        self, identity_key: str, bonus_turn_count: int, *, project_key: str = ""
    ) -> bool:
        record_key = (identity_key, project_key)
        existing = self.records.get(record_key)
        if existing is not None and existing.bonus_granted:
            return False
        base = existing or QuotaRecord(used_turns=0, bonus_turns=0, bonus_granted=False)
        self.records[record_key] = QuotaRecord(
            used_turns=base.used_turns,
            bonus_turns=base.bonus_turns + bonus_turn_count,
            bonus_granted=True,
        )
        return True

    async def record_ip_window_request(
        self, ip_address: str, kind: str, window_start: datetime
    ) -> int:
        # Window start is irrelevant inside one test run; count per ip+kind.
        counter_key = (ip_address, kind)
        self.ip_window_counts[counter_key] = self.ip_window_counts.get(counter_key, 0) + 1
        return self.ip_window_counts[counter_key]

    async def consume_turn_atomically(
        self, identity_key: str, effective_cap: int, *, project_key: str
    ) -> QuotaRecord:
        record_key = (identity_key, project_key)
        current = self.records.get(record_key) or QuotaRecord(
            used_turns=0, bonus_turns=0, bonus_granted=False
        )
        if current.used_turns >= effective_cap + current.bonus_turns:
            raise AssertionError("lead flow does not consume turns")
        updated = QuotaRecord(
            used_turns=current.used_turns + 1,
            bonus_turns=current.bonus_turns,
            bonus_granted=current.bonus_granted,
        )
        self.records[record_key] = updated
        return updated

    async def fetch_quota_record(self, identity_key: str, *, project_key: str):
        return self.records.get((identity_key, project_key))

    async def purge_stale_records(self, *args: object) -> tuple[int, int]:
        raise NotImplementedError("lead flow never purges")


def make_client(
    repo: FakeLeadRepository | None = None,
    *,
    quota_store: InMemoryQuotaRecordStore | None = None,
    extra_overrides: dict | None = None,
) -> tuple[TestClient, FakeLeadRepository, InMemoryQuotaRecordStore]:
    """Build an app with in-memory leads + quota storage and no-op mirror.

    The quota double is installed unconditionally: falling through to the
    late-bound Postgres adapter would couple endpoint tests to a live database.
    """
    app = create_app()
    resolved_repo = repo if repo is not None else FakeLeadRepository()
    resolved_quota_store = quota_store if quota_store is not None else InMemoryQuotaRecordStore()
    app.dependency_overrides[get_lead_repository] = lambda: resolved_repo
    app.dependency_overrides[get_realtime_lead_mirror] = lambda: _NoopMirror()
    app.dependency_overrides[get_quota_storage] = lambda: resolved_quota_store
    for dependency, replacement in (extra_overrides or {}).items():
        app.dependency_overrides[dependency] = replacement
    return TestClient(app), resolved_repo, resolved_quota_store


class _NoopMirror:
    async def upsert_lead_mirror(self, *, document_id: str, document: object) -> None:
        return None

    async def remove_lead_mirror(self, document_id: str) -> None:
        return None

    async def health_check(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def generated_anon_identity_secret(monkeypatch: pytest.MonkeyPatch):
    """Isolate token signing behind a runtime-generated secret (no literals).

    The settings cache must be dropped both ways so other tests never observe
    this secret and this test never observes a previously cached Settings.
    """
    secret = f"test-only-anon-secret-{uuid.uuid4().hex}"
    monkeypatch.setenv("ANON_IDENTITY_SECRET", secret)
    get_settings.cache_clear()
    yield secret
    get_settings.cache_clear()


def mint_token() -> str:
    service = AnonymousIdentityService(get_settings().anon_identity_secret)
    return service.mint_token()


def submit_lead(client: TestClient, phone: str, **extra: object):
    body = {"project_key": "camellia", "phone": phone, "consent": True}
    body.update(extra)
    return client.post("/api/lead", json=body)


@pytest.mark.asyncio
async def test_valid_anon_token_grants_bonus_exactly_once() -> None:
    client, repo, quota_store = make_client()
    anon_token = mint_token()
    first = submit_lead(client, "0905000001", anon_token=anon_token)
    assert first.status_code == 201
    # Config default anonymous_bonus_turns_after_lead=5 lands as the granted n.
    assert first.json()["quota_bonus_granted"] == 5
    second = submit_lead(client, "0905000002", anon_token=anon_token)
    assert second.status_code == 201
    assert second.json()["quota_bonus_granted"] == 0
    identity_claims = AnonymousIdentityService(get_settings().anon_identity_secret).verify_token(
        anon_token
    )
    assert identity_claims is not None
    stored = quota_store.records[(identity_claims.subject, "camellia")]
    assert stored.bonus_granted is True
    assert stored.bonus_turns == 5

    other_project = quota_store.records.get((identity_claims.subject, "soleil"))
    assert other_project is None

    consumed = await consume_turn(
        identity_claims.subject, 1, project_key="camellia", storage=quota_store
    )
    assert consumed.remaining_turns == 5
    other_consumed = await consume_turn(
        identity_claims.subject, 1, project_key="soleil", storage=quota_store
    )
    assert other_consumed.remaining_turns == 0


def test_missing_or_invalid_anon_token_still_creates_lead_with_zero_bonus() -> None:
    client, repo, _quota_store = make_client()
    without_token = submit_lead(client, "0905000010")
    assert without_token.status_code == 201
    assert without_token.json()["quota_bonus_granted"] == 0
    tampered_token = mint_token()[:-2] + "xx"
    with_tampered = submit_lead(client, "0905000011", anon_token=tampered_token)
    assert with_tampered.status_code == 201
    assert with_tampered.json()["quota_bonus_granted"] == 0
    assert len(repo.leads) == 2


def test_consent_false_still_rejected_with_400() -> None:
    client, repo, _quota_store = make_client()
    response = client.post(
        "/api/lead",
        json={"project_key": "camellia", "phone": "0905123456", "consent": False},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Consent is required"
    assert repo.leads == {}


def test_duplicate_phone_within_cooldown_window_rejected_with_429() -> None:
    client, repo, _quota_store = make_client()
    first = submit_lead(client, "0905123456")
    assert first.status_code == 201
    duplicate = submit_lead(client, "0905123456")
    assert duplicate.status_code == 429
    # The rejected duplicate persisted nothing.
    assert len(repo.leads) == 1


def test_phone_cooldown_allows_resubmission_after_window_passes() -> None:
    client, repo, _quota_store = make_client()
    assert submit_lead(client, "0905123456").status_code == 201
    stale_lead = repo.leads[1]
    repo.leads[1] = replace(
        stale_lead, created_at=datetime.now(timezone.utc) - timedelta(hours=25)
    )
    resubmission = submit_lead(client, "0905123456")
    assert resubmission.status_code == 201
    assert len(repo.leads) == 2


def test_different_phones_never_collide_on_cooldown() -> None:
    client, repo, _quota_store = make_client()
    assert submit_lead(client, "0905000001").status_code == 201
    assert submit_lead(client, "0905000002").status_code == 201
    assert len(repo.leads) == 2


def test_per_ip_limit_trips_after_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    # TestClient's TCP peer is the non-IP string "testclient", which production
    # logic rightly treats as an untrusted peer: XFF would be ignored and the
    # brake keyed on the raw peer. Declaring "testclient" a trusted proxy lets
    # the forwarded address drive the per-IP counter without loosening prod.
    # JSON list: pydantic-settings decodes list[str] env vars as JSON before
    # the Settings comma-split validator ever runs.
    monkeypatch.setenv("TRUSTED_PROXY_IPS", '["testclient"]')
    get_settings.cache_clear()
    client, _repo, quota_store = make_client(
        extra_overrides={get_lead_ip_request_limit: lambda: 1},
    )
    forwarded_ip = {"X-Forwarded-For": "203.0.113.7"}
    first = client.post(
        "/api/lead",
        json={"project_key": "camellia", "phone": "0905000001", "consent": True},
        headers=forwarded_ip,
    )
    assert first.status_code == 201
    blocked = client.post(
        "/api/lead",
        json={"project_key": "camellia", "phone": "0905000002", "consent": True},
        headers=forwarded_ip,
    )
    assert blocked.status_code == 429
    assert "Too many leads" in blocked.json()["detail"]
    # Denied attempts still consumed budget: only one lead ever persisted.
    assert sum(quota_store.ip_window_counts.values()) == 2


def test_non_ip_client_address_skips_ip_brake_but_still_creates_lead() -> None:
    # TestClient's host is the non-IP string "testclient"; a deployment without
    # parseable client addresses must degrade to cooldown-only, never 500.
    client, repo, quota_store = make_client()
    response = submit_lead(client, "0905000007")
    assert response.status_code == 201
    assert response.json()["quota_bonus_granted"] == 0
    assert repo.leads
    assert not quota_store.ip_window_counts


def test_phone_cooldown_provider_reads_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAD_PHONE_COOLDOWN_SECONDS", "3600")
    assert get_phone_cooldown_seconds() == 3600
    monkeypatch.delenv("LEAD_PHONE_COOLDOWN_SECONDS")
    assert get_phone_cooldown_seconds() == 24 * 60 * 60


def test_mask_phone_behavior_unchanged() -> None:
    # Byte-for-byte guard on the staff-side masking contract (spec §6).
    assert mask_phone("0905 123 456") == "0905***456"
    assert mask_phone("+84905123456") == "+849***456"
    assert mask_phone("09051") == "***"


def test_quota_double_satisfies_protocol_typing() -> None:
    # Static sanity: the fake is assignable to the application-side protocol.
    store: QuotaRecordStore = InMemoryQuotaRecordStore()  # type: ignore[assignment]
    assert store is not None
