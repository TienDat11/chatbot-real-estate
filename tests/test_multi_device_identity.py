"""Multi-device identity linking (2026-09-09) regression tests.

Ground truth: signing the same Firebase account in on a second device used to
break /query usage — the link endpoint 409'd every later device because
identity_links.firebase_uid was UNIQUE and the store could not represent two
devices' anon identities on one account. Four layers are pinned here:

1. service contract over a fake store (idempotent re-link, 409 re-bind),
2. HTTP route via the offline auth seams (200/200/409 progression),
3. real-Postgres adapter semantics (skips gracefully when PG is unreachable),
4. static migration/schema contracts for the dropped UNIQUE constraint.

No live Firebase, no live LLM: tokens are minted with the shared local RSA
JWKS seams and the same-process ephemeral anon signing secret.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.quota_service import link_identity
from api.infrastructure.config.config import get_settings
from api.interfaces.api import auth_routes
from api.interfaces.api.main import create_app
from tests._auth_seams import (
    apply_offline_auth_seams,
    base_claims,
    build_offline_verifier,
    mint_id_token,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IDENTITY_LINKING_MIGRATION = _REPO_ROOT / "db" / "migrations" / "2026-08-25-identity-linking.sql"
_MULTI_DEVICE_MIGRATION = (
    _REPO_ROOT / "db" / "migrations" / "2026-09-09-multi-device-identity-links.sql"
)
_SCHEMA_SQL = _REPO_ROOT / "db" / "schema.sql"


# --------------------------------------------------------------------------- #
# 1. Service contract over a fake store (no infra at all)
# --------------------------------------------------------------------------- #


class FakeLinkStore:
    """Minimal QuotaRecordStore stand-in recording link_identity_atomically."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._links: dict[str, str] = {}

    async def link_identity_atomically(self, anon_identity_key: str, firebase_uid: str) -> bool:
        self.calls.append((anon_identity_key, firebase_uid))
        # Mirror the migrated SQL: PK(anon_identity_key) only, so N devices ->
        # one account is representable; one anon key -> one account only.
        return self._links.setdefault(anon_identity_key, firebase_uid) == firebase_uid


@pytest.mark.asyncio
async def test_service_contract_second_device_links_and_relink_idempotent():
    store = FakeLinkStore()

    device_a = "multi-device-test-device-a"
    device_b = "multi-device-test-device-b"
    uid = "quota-test-firebase-uid"

    # Two devices, one account: both link successfully (pre-fix, the UNIQUE
    # firebase_uid made the second insert conflict -> False -> route 409).
    assert await link_identity(device_a, uid, storage=store) is True
    assert await link_identity(device_b, uid, storage=store) is True
    assert store.calls == [(device_a, uid), (device_b, uid)]

    # Idempotent re-link of the SAME pair: success (the store decides; the
    # service layer forwards rather than failing 409).
    assert await link_identity(device_a, uid, storage=store) is True
    assert store.calls[-1] == (device_a, uid)

    # Re-binding one device to a DIFFERENT account stays fail-closed.
    assert await link_identity(device_a, "quota-test-other-uid", storage=store) is False


@pytest.mark.asyncio
async def test_service_contract_rejects_blank_keys():
    store = FakeLinkStore()
    with pytest.raises(ValueError):
        await link_identity("   ", "quota-test-uid", storage=store)
    with pytest.raises(ValueError):
        await link_identity("multi-device-test-k", "  ", storage=store)


# --------------------------------------------------------------------------- #
# 2. HTTP route via offline auth seams
# --------------------------------------------------------------------------- #


def _bearer_for(local_rsa_jwk: dict, uid: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + mint_id_token(local_rsa_jwk, base_claims(uid, "customer"))
    }


@pytest_asyncio.fixture()
async def link_client(monkeypatch, local_rsa_jwk, offline_auth_seams):
    del offline_auth_seams  # autowires the offline Firebase verifier seams
    identity_service = AnonymousIdentityService(get_settings().anon_identity_secret)

    calls: list[tuple[str, str]] = []
    links: dict[str, str] = {}

    async def fake_link_identity(anon_identity_key: str, firebase_uid: str) -> bool:
        calls.append((anon_identity_key, firebase_uid))
        return links.setdefault(anon_identity_key, firebase_uid) == firebase_uid

    monkeypatch.setattr(auth_routes, "link_identity", fake_link_identity)

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client, identity_service, calls


@pytest_asyncio.fixture()
async def link_client(monkeypatch, local_rsa_jwk):
    # auth_routes binds get_firebase_auth_verifier / get_staff_audit_store at
    # import time, so the offline verifier must be installed on BOTH the DI
    # module (offline_auth_seams) and the route module's own names.
    apply_offline_auth_seams(monkeypatch, local_rsa_jwk)
    from api.infrastructure import dependencies as dependency_injection
    from tests._auth_seams import build_offline_verifier

    offline_verifier = build_offline_verifier(local_rsa_jwk)
    monkeypatch.setattr(
        "api.interfaces.api.auth_routes.get_firebase_auth_verifier",
        lambda: offline_verifier,
    )

    class _NullAuditStore:
        async def record_entry(self, _entry) -> None:
            return None

    monkeypatch.setattr(
        "api.interfaces.api.auth_routes.get_staff_audit_store",
        lambda: _NullAuditStore(),
    )
    identity_service = AnonymousIdentityService(get_settings().anon_identity_secret)

    calls: list[tuple[str, str]] = []
    links: dict[str, str] = {}

    async def fake_link_identity(anon_identity_key: str, firebase_uid: str) -> bool:
        calls.append((anon_identity_key, firebase_uid))
        return links.setdefault(anon_identity_key, firebase_uid) == firebase_uid

    monkeypatch.setattr(auth_routes, "link_identity", fake_link_identity)

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client, identity_service, calls


def _bearer_for(local_rsa_jwk: dict, uid: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + mint_id_token(local_rsa_jwk, base_claims(uid, "customer"))
    }


@pytest.mark.asyncio
async def test_route_second_device_links_then_409_only_on_rebind(link_client, local_rsa_jwk):
    client, identity_service, calls = link_client
    uid = "uid-multi-device"
    headers = _bearer_for(local_rsa_jwk, uid)
    token_a = identity_service.mint_token()
    token_b = identity_service.mint_token()

    # Device A links: 200.
    first = await client.post(
        "/api/auth/link-anon", json={"anon_token": token_a}, headers=headers
    )
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is True
    assert body["firebase_uid"] == uid
    assert body["identity_key"]

    # Device B, same account: 200 (the bug being fixed — pre-fix 409).
    second = await client.post(
        "/api/auth/link-anon", json={"anon_token": token_b}, headers=headers
    )
    assert second.status_code == 200

    # Same pair relinked: idempotent 200, no duplicate row attempted.
    again = await client.post(
        "/api/auth/link-anon", json={"anon_token": token_a}, headers=headers
    )
    assert again.status_code == 200

    # Rebinding device A to a DIFFERENT account: 409.
    rebind_headers = _bearer_for(local_rsa_jwk, "uid-multi-device-other")
    rebind = await client.post(
        "/api/auth/link-anon", json={"anon_token": token_a}, headers=rebind_headers
    )
    # The store sees identity KEYS (token subjects), one call per request:
    # device A, device B, device A again, device A rebind attempt.
    subject_a = identity_service.verify_token(token_a).subject
    subject_b = identity_service.verify_token(token_b).subject
    assert [key for key, _uid in calls] == [subject_a, subject_b, subject_a, subject_a]


@pytest.mark.asyncio
async def test_route_requires_firebase_bearer_and_valid_anon_token(link_client, local_rsa_jwk):
    client, identity_service, _calls = link_client
    token = identity_service.mint_token()

    missing_bearer = await client.post("/api/auth/link-anon", json={"anon_token": token})
    assert missing_bearer.status_code == 401

    bad_bearer = await client.post(
        "/api/auth/link-anon",
        json={"anon_token": token},
        headers={"Authorization": "Bearer not-a-firebase-token"},
    )
    assert bad_bearer.status_code == 401

    headers = _bearer_for(local_rsa_jwk, "uid-multi-device")
    invalid_anon = await client.post(
        "/api/auth/link-anon", json={"anon_token": "tampered.token"}, headers=headers
    )
    assert invalid_anon.status_code == 422

    staff = await client.post(
        "/api/auth/link-anon",
        json={"anon_token": token},
        headers={
            "Authorization": "Bearer "
            + mint_id_token(local_rsa_jwk, base_claims("uid-staff", "sales"))
        },
    )
    assert staff.status_code == 403


# --------------------------------------------------------------------------- #
# 3. Real Postgres adapter semantics (graceful skip without PG)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def migration_texts() -> tuple[str, str]:
    return (
        _IDENTITY_LINKING_MIGRATION.read_text(encoding="utf-8"),
        _MULTI_DEVICE_MIGRATION.read_text(encoding="utf-8"),
    )


@pytest.mark.asyncio
async def test_postgres_adapter_multi_device_semantics(migration_texts):
    asyncpg = pytest.importorskip("asyncpg")

    from asyncpg import exceptions as asyncpg_exceptions  # noqa: PLC0415

    from api.infrastructure.adapters.postgres_leads import close_lead_pool  # noqa: PLC0415
    from api.infrastructure.adapters.postgres_quota import PostgresQuotaStore  # noqa: PLC0415

    dsn = get_settings().pg_dsn
    if not dsn:
        pytest.skip("no pg_dsn configured")

    pool = None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, timeout=10)
    except Exception:  # noqa: BLE001 — Docker/PG down: repo-established skip
        pytest.skip("Postgres unreachable; DB-backed identity tests skip")

    stamp = int(time.time())
    anon_keys = [f"multi-device-test-{stamp}-{index}" for index in range(3)]
    uid = f"quota-test-uid-{stamp}"
    other_uid = f"quota-test-other-{stamp}"
    account_key = f"firebase:{uid}"
    other_account_key = f"firebase:{other_uid}"
    try:
        await pool.execute(migration_texts[0])
        await pool.execute(migration_texts[1])
        store = PostgresQuotaStore()

        # Seed device A usage in its anon bucket.
        await pool.execute(
            """INSERT INTO anon_quota
                 (identity_key, project_key, used_turns, bonus_turns, granted_turns, bonus_granted)
               VALUES ($1, 'camellia', 2, 1, 0, false)""",
            anon_keys[0],
        )

        # Device A links: fresh pair -> merged exactly once.
        assert await store.link_identity_atomically(anon_keys[0], uid) is True
        merged = await pool.fetchrow(
            "SELECT used_turns, bonus_turns FROM anon_quota"
            " WHERE identity_key = $1 AND project_key = 'camellia'",
            account_key,
        )
        assert merged is not None
        assert merged["used_turns"] == 2
        assert merged["bonus_turns"] == 1
        assert await pool.fetchval(
            "SELECT count(*) FROM anon_quota WHERE identity_key = $1", anon_keys[0]
        ) == 0

        # Device B, same account: fresh pair -> True (pre-fix UNIQUE 409'd here).
        assert await store.link_identity_atomically(anon_keys[1], uid) is True
        # Idempotent re-link of device A: True, no audit spam, no double merge.
        assert await store.link_identity_atomically(anon_keys[0], uid) is True
        audit_count = await pool.fetchval(
            "SELECT count(*) FROM identity_link_audit WHERE anon_identity_key = $1",
            anon_keys[0],
        )
        assert audit_count == 1
        assert (
            await pool.fetchval(
                "SELECT used_turns FROM anon_quota"
                " WHERE identity_key = $1 AND project_key = 'camellia'",
                account_key,
            )
            == 2
        )

        # Re-bind device A to a different account: False (route 409s).
        assert await store.link_identity_atomically(anon_keys[0], other_uid) is False

        # N devices -> one account is representable; row count proves no
        # UNIQUE(firebase_uid) remains.
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM identity_links WHERE firebase_uid = $1", uid
            )
            == 2
        )
        # Unique-constraint absence is enforced behaviorally: a second anon key
        # re-binding to other_uid succeeds...
        assert await store.link_identity_atomically(anon_keys[2], other_uid) is True
        # ...while one anon key can never hold two accounts (PK enforced).
        with pytest.raises(asyncpg_exceptions.UniqueViolationError):
            async with pool.acquire() as connection:
                await connection.execute(
                    "INSERT INTO identity_links (anon_identity_key, firebase_uid)"
                    " VALUES ($1, $2)",
                    anon_keys[0],
                    other_uid,
                )
    finally:
        if pool is not None:
            await pool.execute(
                "DELETE FROM identity_link_audit WHERE anon_identity_key = ANY($1)",
                anon_keys,
            )
            await pool.execute(
                "DELETE FROM identity_links WHERE anon_identity_key = ANY($1)",
                anon_keys,
            )
            await pool.execute(
                "DELETE FROM anon_quota WHERE identity_key = ANY($1) OR identity_key = ANY($2)",
                anon_keys,
                [account_key, other_account_key],
            )
            await pool.close()
            await close_lead_pool()


# --------------------------------------------------------------------------- #
# 4. Static migration/schema contracts
# --------------------------------------------------------------------------- #


def test_migration_drops_firebase_uid_unique_only():
    sql = _MULTI_DEVICE_MIGRATION.read_text(encoding="utf-8").lower()
    assert "drop constraint if exists identity_links_firebase_uid_key" in sql
    # Forward-only guard rails (repo convention from test_quota_migration_contract).
    assert "drop table" not in sql
    assert "drop schema" not in sql
    assert "truncate" not in sql


def test_schema_sql_describes_post_migration_identity_links_shape():
    sql = _SCHEMA_SQL.read_text(encoding="utf-8").lower()
    assert "create table if not exists identity_links" in sql
    # The unique modifier must be gone from the post-migration shape.
    block = sql.split("create table if not exists identity_links", 1)[1].split(");", 1)[0]
    assert "unique" not in block
    assert "firebase_uid text not null" in block


def test_identity_linking_migration_keeps_historical_content():
    # The 08-25 migration file keeps its historical content; the drop lives in
    # its own forward-only file.
    sql = _IDENTITY_LINKING_MIGRATION.read_text(encoding="utf-8").lower()
    assert "firebase_uid text not null unique" in sql
