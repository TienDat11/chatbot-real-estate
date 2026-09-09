"""Secure wave Issue 2: /query server-enforced anonymous quota contracts.

Fully offline: the pipeline and project scope are stubbed, the quota store is
an in-memory stand-in with real conditional-update semantics, staff tokens
are minted against a locally generated RSA JWKS (same pattern as
test_admin_auth.py), and the IP brake is a fresh process-local store per test
— no LLM, Postgres, or network is touched.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import time

import jwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm

from api.application.services.anon_identity import (
    AnonymousIdentityService,
    InMemoryRateLimitStore,
    set_rate_limit_port,
    verify_anonymous_identity_token,
)
from api.application.services.query_quota_gate import (
    QUOTA_EXCEEDED_ERROR_CODE,
    QUOTA_STORE_UNAVAILABLE_ERROR_CODE,
    SALES_MAPPING_MISSING_ERROR_CODE,
    QueryQuotaBlockedError,
    QueryQuotaGate,
)
from api.application.services.quota_service import (
    QuotaRecord,
    get_snapshot,
)
from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.adapters import firebase_auth_jwks
from api.infrastructure.ports.firebase_auth import VerifiedFirebaseUser
from api.interfaces.api.main import create_app

PROJECT_ID = "sale-chat-bot-11e49"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"

# RFC 2544 benchmark range: valid INET values that cannot collide with a
# real customer address when the Postgres brake is wired locally.
_TEST_IP_CLASS_PREFIX = "198.18."

_PAYLOAD_TEMPLATE = {
    "answer": "ok",
    "sources": [],
    "facts": [],
    "places": [],
    "confidence": "HIGH",
    "requires_review": False,
    "routing": {"intent": "rag"},
    "trace_id": "t-1",
    "latency_ms": 1,
}


class FakeRecordingPipeline:
    """Minimal pipeline stand-in; the handler attaches quota afterwards."""

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        return dict(_PAYLOAD_TEMPLATE)


class RejectingPipeline(FakeRecordingPipeline):
    """Pipeline that rejects the query (spec §4 R1 refund path)."""

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        from api.application.pipelines.workflow import QueryRejected  # noqa: PLC0415

        raise QueryRejected("khong tim thay noi dung phu hop")


class CrashingPipeline(FakeRecordingPipeline):
    """Pipeline that blows up mid-flight (spec §4 R1 crash-refund path)."""

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        raise RuntimeError("rag leg crashed")


class StubAnonymousQuotaStore:
    """In-memory QuotaRecordStore keeping the conditional-update semantics.

    The sleep(0) yield makes gathered submissions genuinely interleave, so
    the race test exercises the same win/lose decision the atomic SQL update
    makes under row lock.
    """

    def __init__(self) -> None:
        self._rows_by_identity: dict[tuple[str, str], dict] = {}

    async def consume_turn_atomically(
        self, identity_key: str, effective_cap: int, *, project_key: str
    ):
        await asyncio.sleep(0)
        row_key = (identity_key, project_key)
        row = self._rows_by_identity.setdefault(
            row_key,
            {"used_turns": 0, "bonus_turns": 0, "bonus_granted": False},
        )
        if row["used_turns"] < effective_cap + row["bonus_turns"]:
            row["used_turns"] += 1
            return QuotaRecord(**row)
        return None

    async def refund_turn_atomically(
        self, identity_key: str, *, project_key: str, reservation_id: str | None = None
    ):
        row = self._rows_by_identity.get((identity_key, project_key))
        if row is None or row["used_turns"] <= 0:
            return None
        row["used_turns"] -= 1
        return QuotaRecord(**row)

    async def grant_one_time_bonus(
        self, identity_key: str, bonus_turn_count: int, *, project_key: str = ""
    ):
        row_key = (identity_key, project_key)
        row = self._rows_by_identity.setdefault(
            row_key,
            {"used_turns": 0, "bonus_turns": 0, "bonus_granted": False},
        )
        if row["bonus_granted"]:
            return False
        row["bonus_granted"] = True
        row["bonus_turns"] += bonus_turn_count
        return True

    async def fetch_quota_record(self, identity_key: str, *, project_key: str):
        row = self._rows_by_identity.get((identity_key, project_key))
        return QuotaRecord(**row) if row else None

    async def record_ip_window_request(self, ip_address: str, kind: str, window_start) -> int:
        return 1  # the gate throttles through RateLimitPort, never this method

    async def purge_stale_records(self, *args) -> tuple[int, int]:
        return (0, 0)


class FailingQuotaStore(StubAnonymousQuotaStore):
    """Quota store that is unreachable — anonymous enforcement must fail closed."""

    async def consume_turn_atomically(
        self, identity_key: str, effective_cap: int, *, project_key: str
    ):
        raise RuntimeError("quota store unreachable")


def new_test_ip_address() -> str:
    return f"{_TEST_IP_CLASS_PREFIX}{random.randint(0, 255)}.{random.randint(1, 254)}"


def _ip_headers(ip_address: str) -> dict:
    # anon_routes.get_client_ip_address prefers X-Forwarded-For, so tests pin
    # the caller address deterministically without touching request.client.
    return {"x-forwarded-for": ip_address}


def parse_sse_frames(text: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for raw_frame in text.strip().split("\n\n"):
        event_name, data_payload = "", {}
        for line in raw_frame.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_payload = json.loads(line[len("data: ") :])
        frames.append((event_name, data_payload))
    return frames


@pytest.fixture()
def quota_store() -> StubAnonymousQuotaStore:
    return StubAnonymousQuotaStore()


@pytest.fixture()
def gate_secret() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture()
def install_gate(monkeypatch, quota_store, gate_secret):
    """Swap the router's composition-root factory for an offline gate."""

    def _install(**overrides) -> QueryQuotaGate:
        gate = QueryQuotaGate(
            signing_secret=overrides.pop("signing_secret", gate_secret),
            base_turn_cap=overrides.pop("base_turn_cap", 3),
            ip_query_request_limit=overrides.pop("ip_query_request_limit", 120),
            ip_identity_mint_limit=overrides.pop("ip_identity_mint_limit", 30),
            ip_window_seconds=overrides.pop("ip_window_seconds", 3600),
            quota_store=quota_store,
            **overrides,
        )
        monkeypatch.setattr(
            "api.application.services.query_quota_gate.build_query_quota_gate",
            lambda: gate,
        )
        return gate

    return _install


@pytest_asyncio.fixture()
async def client(monkeypatch, install_gate):
    install_gate()
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv",
        FakeRecordingPipeline,
    )
    # httpx ASGITransport presents a (127.0.0.1, 123) peer; trust it so the
    # tests' X-Forwarded-For headers are honored (the same way a deployed app
    # trusts its configured reverse-proxy peer).
    import api.interfaces.api.anon_routes as anon_routes_module

    monkeypatch.setattr(anon_routes_module, "get_trusted_proxy_ips", lambda: {"127.0.0.1"})

    async def _fake_resolve(project_key, *, active_projects=None):
        return project_key or "camellia"

    monkeypatch.setattr("api.application.services.project_scope.resolve_project_key", _fake_resolve)
    # Fresh process-local brake per test so IP buckets never leak between tests.
    set_rate_limit_port(InMemoryRateLimitStore())

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as async_client:
        yield async_client


async def post_query(client: AsyncClient, ip_address: str, **fields):
    return await client.post(
        "/query",
        json={"query": "can P-01 gia bao nhieu?", "project_key": "camellia", **fields},
        headers=_ip_headers(ip_address),
    )


@pytest.mark.asyncio
async def test_fresh_anonymous_identity_three_turns_then_structured_429(
    client, quota_store, gate_secret
):
    """AC1: three useful turns carry correct quota; turn 4 hits the wall."""
    ip_address = new_test_ip_address()
    expected_progression = [(1, 2), (2, 1), (3, 0)]
    persisted_token: str | None = None

    for turn_index, (used_expected, remaining_expected) in enumerate(expected_progression):
        fields = {"anon_token": persisted_token} if persisted_token else {}
        resp = await post_query(client, ip_address, **fields)
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "ok"
        assert body["quota"] == {
            "used_turns": used_expected,
            "remaining_turns": remaining_expected,
            "cap": 3,
            "is_authenticated": False,
            "bonus_granted": 0,
        }
        if turn_index == 0:
            # Self-heal path: first response mints and echoes a usable token.
            persisted_token = body["anon_token"]
            assert persisted_token
            verified = verify_anonymous_identity_token(persisted_token, gate_secret)
            assert verified is not None
        else:
            assert body.get("anon_token") is None

    exhausted = await post_query(client, ip_address, anon_token=persisted_token)
    assert exhausted.status_code == 429
    body = exhausted.json()
    # Exact §5.2 envelope: nothing more, nothing less.
    assert set(body) == {"ok", "error", "lead_cta"}
    assert set(body["error"]) == {"code", "message", "quota"}
    assert body["ok"] is False
    assert body["error"]["code"] == QUOTA_EXCEEDED_ERROR_CODE
    assert body["error"]["quota"]["used_turns"] == 3
    assert body["error"]["quota"]["remaining_turns"] == 0
    assert body["lead_cta"] == {"required": True}
    assert body["error"]["message"].strip()


@pytest.mark.asyncio
async def test_invalid_anon_token_self_heals_into_fresh_minted_identity(
    client, quota_store, gate_secret
):
    """US-3: tampered tokens never error; they are replaced by a fresh mint."""
    resp = await post_query(client, new_test_ip_address(), anon_token="tampered.forge.sig")
    assert resp.status_code == 200
    body = resp.json()

    replacement_token = body["anon_token"]
    assert replacement_token
    assert replacement_token != "tampered.forge.sig"
    verified_claims = verify_anonymous_identity_token(replacement_token, gate_secret)
    assert verified_claims is not None
    assert body["quota"]["used_turns"] == 1


@pytest.mark.asyncio
async def test_sales_bearer_unlimited_past_old_cap_without_quota_writes(
    client, monkeypatch, quota_store, local_rsa_jwk
):
    """Active-mapped sales bypass quota entirely (unlimited past the old 10-turn
    cap) and never touch the quota store — no sales row is ever written."""
    _module_rsa_jwk, private_key = local_rsa_jwk
    now = int(time.time())
    sales_id_token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-sales-query",
            "user_id": "uid-sales-query",
            "email": "uid-sales-query@example.com",
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
            "firebase": {"sign_in_provider": "password"},
            "role": "sales",
        },
        key=private_key,
        algorithm="RS256",
        headers={"kid": "local-test-key"},
    )

    async def fake_key_for_kid(kid: str):
        if kid != "local-test-key":
            return None
        jwk_entry = {k: v for k, v in _module_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

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

    async def active_sales_mapping(_: str) -> int:
        return 42

    monkeypatch.setattr(
        "api.interfaces.api.deps.admin.resolve_assigned_sales_id", active_sales_mapping
    )

    ip_address = new_test_ip_address()
    for _turn_index in range(15):  # well past the old finite cap of 10
        resp = await client.post(
            "/query",
            json={"query": "huong dan tu van", "project_key": "camellia"},
            headers={**_ip_headers(ip_address), "Authorization": f"Bearer {sales_id_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["quota"] == {
            "used_turns": None,
            "remaining_turns": None,
            "cap": None,
            "is_authenticated": True,
            "bonus_granted": None,
        }
        assert body.get("anon_token") is None
        # Sales turns must never surface the customer lead wall or bonus CTA.
        assert body.get("lead_cta_hint") is None

    # No quota row persisted for the sales identity: unlimited staff turns are
    # served without writes, so no per-identity store state is ever created.
    assert ("firebase:uid-sales-query", "camellia") not in quota_store._rows_by_identity


@pytest.mark.asyncio
async def test_customer_bearer_keeps_finite_five_turn_cap(
    client, monkeypatch, quota_store, local_rsa_jwk
):
    """Registered customers keep the 5-turn policy unchanged — staff unlimited
    must not leak to the customer machine."""
    _module_rsa_jwk, private_key = local_rsa_jwk
    now = int(time.time())
    customer_id_token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-customer-query",
            "user_id": "uid-customer-query",
            "email": "uid-customer-query@example.com",
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
            "firebase": {"sign_in_provider": "password"},
            "role": "customer",
        },
        key=private_key,
        algorithm="RS256",
        headers={"kid": "local-test-key"},
    )

    async def fake_key_for_kid(kid: str):
        if kid != "local-test-key":
            return None
        jwk_entry = {k: v for k, v in _module_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

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

    ip_address = new_test_ip_address()
    for turn_index in range(5):
        resp = await client.post(
            "/query",
            json={"query": "gia can", "project_key": "camellia"},
            headers={**_ip_headers(ip_address), "Authorization": f"Bearer {customer_id_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["quota"]["cap"] == 5
        assert body["quota"]["used_turns"] == turn_index + 1
        assert body["quota"]["remaining_turns"] == 4 - turn_index
        assert body["quota"]["is_authenticated"] is True

    # Turn 6 hits the customer wall: the 5-turn cap is intact.
    walled = await client.post(
        "/query",
        json={"query": "them mot cau nua", "project_key": "camellia"},
        headers={**_ip_headers(ip_address), "Authorization": f"Bearer {customer_id_token}"},
    )
    assert walled.status_code == 429
    wall_body = walled.json()
    assert wall_body["error"]["code"] == QUOTA_EXCEEDED_ERROR_CODE
    assert wall_body["error"]["quota"]["used_turns"] == 5
    assert wall_body["lead_cta"] == {"required": True}


@pytest.mark.asyncio
async def test_training_sales_bearer_unlimited_with_auth_layer_verification(
    client, monkeypatch, quota_store, local_rsa_jwk
):
    """answer_mode=training keeps the server-side sales auth gate (route-level
    401/403 for unmapped/inactive) AND gives the active-mapped sales principal
    unlimited turns through the real quota gate — no training cap, no CTA."""
    _module_rsa_jwk, private_key = local_rsa_jwk
    now = int(time.time())
    sales_id_token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": PROJECT_ID,
            "sub": "uid-sales-training",
            "user_id": "uid-sales-training",
            "email": "uid-sales-training@example.com",
            "email_verified": True,
            "iat": now,
            "exp": now + 3600,
            "firebase": {"sign_in_provider": "password"},
            "role": "sales",
        },
        key=private_key,
        algorithm="RS256",
        headers={"kid": "local-test-key"},
    )

    async def fake_key_for_kid(kid: str):
        if kid != "local-test-key":
            return None
        jwk_entry = {k: v for k, v in _module_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

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

    async def active_sales_mapping(_: str) -> int:
        return 42

    monkeypatch.setattr(
        "api.interfaces.api.deps.admin.resolve_assigned_sales_id", active_sales_mapping
    )

    ip_address = new_test_ip_address()
    for _turn_index in range(12):  # well past the old finite cap of 10
        resp = await client.post(
            "/query",
            json={
                "query": "coaching",
                "project_key": "camellia",
                "answer_mode": "training",
                "session_id": "training-quota-unlimited",
                "context": {"project_key": "camellia"},
            },
            headers={**_ip_headers(ip_address), "Authorization": f"Bearer {sales_id_token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["quota"] == {
            "used_turns": None,
            "remaining_turns": None,
            "cap": None,
            "is_authenticated": True,
            "bonus_granted": None,
        }
        # Training isolation: the customer lead CTA never leaks to staff.
        assert body.get("lead_cta_hint") is None
        assert body.get("project_redirect") is None
        assert body.get("anon_token") is None

    # Unlimited staff training writes no quota row for the sales identity.
    assert ("firebase:uid-sales-training", "_training") not in quota_store._rows_by_identity


class _FakeFirebaseVerifier:
    def __init__(self, user: VerifiedFirebaseUser):
        self.user = user

    async def verify_id_token(self, _token: str) -> VerifiedFirebaseUser:
        return self.user


class _FailingFirebaseVerifier:
    async def verify_id_token(self, _token: str) -> VerifiedFirebaseUser:
        raise RuntimeError("stale token")


def _verified_user(*, role: str | None, uid: str = "uid-query") -> VerifiedFirebaseUser:
    return VerifiedFirebaseUser(
        firebase_uid=uid,
        email=f"{uid}@example.test",
        email_verified=True,
        role=role,
        auth_provider="password",
        token_issued_at=None,
        token_expires_at=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapping_error, expected_status",
    [("missing", 403), ("unavailable", 503)],
)
async def test_sales_mapping_failure_is_fail_closed(
    monkeypatch, gate_secret, mapping_error, expected_status
):
    gate = QueryQuotaGate(
        signing_secret=gate_secret,
        ip_query_request_limit=120,
        ip_identity_mint_limit=30,
        ip_window_seconds=3600,
    )
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: _FakeFirebaseVerifier(_verified_user(role="sales")),
    )

    from api.interfaces.api.deps import admin as admin_deps

    async def mapping(_uid: str) -> int:
        if mapping_error == "missing":
            raise admin_deps.SalesMappingMissing
        raise admin_deps.SalesMappingUnavailable

    monkeypatch.setattr(admin_deps, "resolve_assigned_sales_id", mapping)
    with pytest.raises(QueryQuotaBlockedError) as caught:
        await gate.prepare_turn(
            presented_anon_token=None,
            bearer_id_token="stale-or-valid",
            client_ip_address="198.18.0.1",
            project_key="camellia",
        )
    assert caught.value.status_code == expected_status
    if expected_status == 403:
        assert caught.value.error_code == SALES_MAPPING_MISSING_ERROR_CODE


@pytest.mark.asyncio
async def test_sales_mapping_missing_cannot_fall_back_to_anonymous(monkeypatch, gate_secret):
    gate = QueryQuotaGate(
        signing_secret=gate_secret,
        ip_query_request_limit=120,
        ip_identity_mint_limit=30,
        ip_window_seconds=3600,
    )
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: _FakeFirebaseVerifier(_verified_user(role="sales")),
    )
    from api.interfaces.api.deps import admin as admin_deps

    async def missing(_uid: str) -> int:
        raise admin_deps.SalesMappingMissing

    monkeypatch.setattr(admin_deps, "resolve_assigned_sales_id", missing)
    with pytest.raises(QueryQuotaBlockedError) as caught:
        await gate.prepare_turn(
            presented_anon_token=None,
            bearer_id_token="valid-signed-token",
            client_ip_address="198.18.0.2",
            project_key="camellia",
        )
    assert caught.value.status_code == 403
    assert caught.value.error_code == SALES_MAPPING_MISSING_ERROR_CODE


@pytest.mark.asyncio
async def test_invalid_firebase_token_remains_anonymous(monkeypatch, gate_secret):
    store = StubAnonymousQuotaStore()
    gate = QueryQuotaGate(
        signing_secret=gate_secret,
        ip_query_request_limit=120,
        ip_identity_mint_limit=30,
        ip_window_seconds=3600,
        quota_store=store,
    )
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: _FailingFirebaseVerifier(),
    )
    context = await gate.prepare_turn(
        presented_anon_token=None,
        bearer_id_token="stale-firebase-token",
        client_ip_address="198.18.0.3",
        project_key="camellia",
    )
    assert context.quota_payload()["is_authenticated"] is False
    assert context.quota_payload()["cap"] == 3


@pytest.mark.asyncio
async def test_admin_remains_unlimited_without_sales_lookup(monkeypatch, gate_secret):
    gate = QueryQuotaGate(
        signing_secret=gate_secret,
        ip_query_request_limit=120,
        ip_identity_mint_limit=30,
        ip_window_seconds=3600,
    )
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: _FakeFirebaseVerifier(_verified_user(role="admin", uid="uid-admin")),
    )
    context = await gate.prepare_turn(
        presented_anon_token=None,
        bearer_id_token="admin-token",
        client_ip_address="198.18.0.4",
        project_key="camellia",
    )
    assert context.quota_payload()["remaining_turns"] is None


@pytest.mark.asyncio
async def test_sse_ack_and_done_carry_reserved_quota(client):
    """Spec §5.3 under reserve-before: the atomic reservation happens before the
    stream starts, so ack and done both carry the post-reservation snapshot."""
    ip_address = new_test_ip_address()
    first = await client.post(
        "/query",
        json={"query": "gia khuyen mai?", "project_key": "camellia"},
        headers={**_ip_headers(ip_address), "Accept": "text/event-stream"},
    )
    assert first.status_code == 200
    frames = parse_sse_frames(first.text)
    event_names = [name for name, _ in frames]
    assert event_names[0] == "ack"
    assert event_names[-1] == "done"
    assert "token" not in event_names

    ack_data = frames[0][1]
    assert ack_data["quota"]["used_turns"] == 1
    assert ack_data["quota"]["remaining_turns"] == 2
    assert ack_data["anon_token"]

    done_data = frames[-1][1]
    assert done_data["answer"] == "ok"
    assert done_data["quota"]["used_turns"] == 1
    assert done_data["quota"]["remaining_turns"] == 2


@pytest.mark.asyncio
async def test_sse_exhausted_emits_error_then_done_without_token_frames(client, gate_secret):
    """Spec §5.3: the wall streams error+done only; no answer tokens leak."""
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    ip_address = new_test_ip_address()
    for _ in range(3):
        resp = await post_query(client, ip_address, anon_token=anon_token)
        assert resp.status_code == 200

    exhausted = await client.post(
        "/query",
        json={"query": "them mot cau nua", "project_key": "camellia", "anon_token": anon_token},
        headers={**_ip_headers(ip_address), "Accept": "text/event-stream"},
    )
    assert exhausted.status_code == 200  # SSE stays 200 by nature
    frames = parse_sse_frames(exhausted.text)
    assert [(name, data.get("code")) for name, data in frames] == [
        ("error", QUOTA_EXCEEDED_ERROR_CODE),
        ("done", None),
    ]
    error_data = frames[0][1]
    assert error_data["quota"]["used_turns"] == 3
    assert error_data["quota"]["remaining_turns"] == 0
    assert error_data["lead_cta"] == {"required": True}
    assert all(name != "token" for name, _ in frames)


@pytest.mark.asyncio
async def test_double_submit_race_at_boundary_consumes_exactly_cap(
    client, quota_store, gate_secret
):
    """AC3: concurrent submissions at the boundary spend exactly one slot."""
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    identity_key = identity_service.verify_token(anon_token).subject
    ip_address = new_test_ip_address()

    for _ in range(2):  # drive the identity to used=2 of cap 3
        warmup = await post_query(client, ip_address, anon_token=anon_token)
        assert warmup.status_code == 200

    responses = await asyncio.gather(
        *(post_query(client, ip_address, anon_token=anon_token) for _ in range(2))
    )
    status_codes = sorted(resp.status_code for resp in responses)
    assert status_codes == [200, 429]

    winner_bodies = [resp.json() for resp in responses if resp.status_code == 200]
    loser_bodies = [resp.json() for resp in responses if resp.status_code == 429]
    assert winner_bodies[0]["quota"]["used_turns"] == 3
    assert loser_bodies[0]["error"]["code"] == QUOTA_EXCEEDED_ERROR_CODE

    final_snapshot = await get_snapshot(
        identity_key, 3, project_key="camellia", storage=quota_store
    )
    assert final_snapshot.used_turns == 3
    assert final_snapshot.remaining_turns == 0


@pytest.mark.asyncio
async def test_unconfigured_secret_degrades_to_legacy_null_quota(client, install_gate):
    """Dev bootability: empty signing secret keeps serving with null quota."""
    install_gate(signing_secret="")
    resp = await post_query(client, new_test_ip_address())
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "ok"
    assert body["quota"] == {
        "used_turns": None,
        "remaining_turns": None,
        "cap": None,
        "is_authenticated": False,
        "bonus_granted": None,
    }
    assert body.get("anon_token") is None


def _install_gate_with(monkeypatch, gate_secret: str, **overrides) -> QueryQuotaGate:
    gate = QueryQuotaGate(
        signing_secret=gate_secret,
        base_turn_cap=overrides.pop("base_turn_cap", 3),
        ip_query_request_limit=overrides.pop("ip_query_request_limit", 120),
        ip_identity_mint_limit=overrides.pop("ip_identity_mint_limit", 30),
        ip_window_seconds=overrides.pop("ip_window_seconds", 3600),
        **overrides,
    )
    monkeypatch.setattr(
        "api.application.services.query_quota_gate.build_query_quota_gate",
        lambda: gate,
    )
    return gate


@pytest.mark.asyncio
async def test_quota_store_failure_refuses_anonymous_turn(client, monkeypatch, gate_secret):
    """Requirement 1: store failure is FAIL-CLOSED for anonymous (JSON)."""
    _install_gate_with(monkeypatch, gate_secret, quota_store=FailingQuotaStore())
    resp = await post_query(client, new_test_ip_address())
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == QUOTA_STORE_UNAVAILABLE_ERROR_CODE
    assert "quota" not in body["error"]
    assert "lead_cta" not in body


@pytest.mark.asyncio
async def test_quota_store_failure_refuses_anonymous_turn_sse(client, monkeypatch, gate_secret):
    """Requirement 1: store failure is FAIL-CLOSED for anonymous (SSE)."""
    _install_gate_with(monkeypatch, gate_secret, quota_store=FailingQuotaStore())
    resp = await client.post(
        "/query",
        json={"query": "gia bao nhieu?", "project_key": "camellia"},
        headers={**_ip_headers(new_test_ip_address()), "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200  # SSE stays 200 by nature
    frames = parse_sse_frames(resp.text)
    assert [(name, data.get("code")) for name, data in frames] == [
        ("error", QUOTA_STORE_UNAVAILABLE_ERROR_CODE),
        ("done", None),
    ]


@pytest.mark.asyncio
async def test_rejected_pipeline_refunds_reserved_turn(
    client, monkeypatch, install_gate, quota_store, gate_secret
):
    """Requirement 2: a rejected pipeline refunds the atomic reservation."""
    install_gate()
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    identity_key = identity_service.verify_token(anon_token).subject
    ip_address = new_test_ip_address()

    # Warmup runs on the fixture's success pipeline so the reservation is real.
    warmup = await post_query(client, ip_address, anon_token=anon_token)
    assert warmup.status_code == 200
    assert warmup.json()["quota"]["used_turns"] == 1

    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv",
        RejectingPipeline,
    )
    rejected = await post_query(client, ip_address, anon_token=anon_token)
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "REJECTED"

    snapshot = await get_snapshot(identity_key, 3, project_key="camellia", storage=quota_store)
    assert snapshot.used_turns == 1  # reservation was refunded
    assert snapshot.remaining_turns == 2


@pytest.mark.asyncio
async def test_crashed_pipeline_sse_refunds_and_emits_error_then_done(
    client, monkeypatch, install_gate, quota_store, gate_secret
):
    """Requirement 2: SSE crash path refunds and never leaks token frames."""
    install_gate()
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv",
        CrashingPipeline,
    )
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    identity_key = identity_service.verify_token(anon_token).subject
    ip_address = new_test_ip_address()

    crash_resp = await client.post(
        "/query",
        json={"query": "huong dan tu van", "project_key": "camellia", "anon_token": anon_token},
        headers={**_ip_headers(ip_address), "Accept": "text/event-stream"},
    )
    assert crash_resp.status_code == 200
    frames = parse_sse_frames(crash_resp.text)
    assert [name for name, _ in frames] == ["ack", "error", "done"]
    assert all(name != "token" for name, _ in frames)

    snapshot = await get_snapshot(identity_key, 3, project_key="camellia", storage=quota_store)
    assert snapshot.used_turns == 0  # fresh identity: reservation fully refunded
    assert snapshot.remaining_turns == 3


@pytest.fixture(scope="module")
def local_rsa_jwk() -> tuple[dict, object]:
    """Module-scoped RSA pair: JWK for the fake resolver + signing key."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-test-key"
    return jwk_entry, private_key


@pytest.mark.asyncio
async def test_per_project_quota_blocks_question_four_in_project_a_only(
    client, quota_store, gate_secret
):
    """B1: exhausting project A walls the 4th question in A, while project B
    keeps its own untouched allowance for the same identity."""
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    ip_address = new_test_ip_address()

    for _ in range(3):
        resp = await post_query(client, ip_address, anon_token=anon_token, project_key="camellia")
        assert resp.status_code == 200

    blocked = await post_query(client, ip_address, anon_token=anon_token, project_key="camellia")
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error"]["code"] == QUOTA_EXCEEDED_ERROR_CODE
    assert body["error"]["quota"]["used_turns"] == 3
    assert body["lead_cta"] == {"required": True}

    fresh_b = await post_query(client, ip_address, anon_token=anon_token, project_key="soleil")
    assert fresh_b.status_code == 200
    assert fresh_b.json()["quota"]["used_turns"] == 1
    assert fresh_b.json()["quota"]["remaining_turns"] == 2


@pytest.mark.asyncio
async def test_per_project_quota_switchback_to_exhausted_project_stays_blocked(
    client, quota_store, gate_secret
):
    """B1: A -> B -> A: project B spends its own allowance and project A stays
    walled — the per-project allowance cannot be recycled by switching."""
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    ip_address = new_test_ip_address()

    for _ in range(3):
        resp = await post_query(client, ip_address, anon_token=anon_token, project_key="camellia")
        assert resp.status_code == 200

    # A is exhausted; B still serves.
    in_b = await post_query(client, ip_address, anon_token=anon_token, project_key="soleil")
    assert in_b.status_code == 200
    assert in_b.json()["quota"]["used_turns"] == 1

    # Back to A: still 429 with the lead CTA.
    back_to_a = await post_query(client, ip_address, anon_token=anon_token, project_key="camellia")
    assert back_to_a.status_code == 429
    back_body = back_to_a.json()
    assert back_body["error"]["code"] == QUOTA_EXCEEDED_ERROR_CODE
    assert back_body["lead_cta"] == {"required": True}


class LeakyCrashingPipeline(FakeRecordingPipeline):
    """Pipeline that crashes with a secret-bearing exception message."""

    async def run(self, query, session_id, as_of, history, on_event=None, **kwargs):
        raise RuntimeError("SECRET-INTERNAL-DETAIL-abc123")


@pytest.mark.asyncio
async def test_sse_crash_error_never_leaks_exception_text(
    client, monkeypatch, install_gate, quota_store, gate_secret
):
    """B4: the SSE error frame carries a stable code, never str(exc)."""
    install_gate()
    monkeypatch.setattr(
        "api.application.pipelines.conv_workflow.RagQueryPipelineConv",
        LeakyCrashingPipeline,
    )
    identity_service = AnonymousIdentityService(gate_secret)
    anon_token = identity_service.mint_token()
    identity_key = identity_service.verify_token(anon_token).subject
    resp = await client.post(
        "/query",
        json={"query": "huong dan tu van", "project_key": "camellia", "anon_token": anon_token},
        headers={**_ip_headers(new_test_ip_address()), "Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    # The raw exception text never reaches the client in ANY frame.
    assert "SECRET-INTERNAL-DETAIL-abc123" not in resp.text
    frames = parse_sse_frames(resp.text)
    error_frames = [(name, data) for name, data in frames if name == "error"]
    assert len(error_frames) == 1
    error_data = error_frames[0][1]
    assert error_data.get("code") == "INTERNAL"
    assert "SECRET-INTERNAL-DETAIL-abc123" not in json.dumps(error_data)
    assert "internal error" in error_data.get("message", "")

    snapshot = await get_snapshot(identity_key, 3, project_key="camellia", storage=quota_store)
    assert snapshot.used_turns == 0  # reservation refunded even on the leak path
