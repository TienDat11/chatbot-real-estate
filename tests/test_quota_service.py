"""Secure wave Issue 1B: durable anon quota + per-IP rate limit stores.

Integration tests run against the local docker compose Postgres configured
through .env (Settings.pg_dsn — never literals here). When that database is
unreachable every database-backed test skips with an explicit reason; the
pure-Python contract tests (bucket math, kind allowlist) always run.

The migration file itself is applied by the fixture because it is fully
additive/idempotent (IF NOT EXISTS), keeping these tests self-sufficient on a
freshly provisioned database.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from api.application.services.quota_service import (
    QuotaExceeded,
    QuotaSnapshot,
    check_ip_limit,
    consume_turn,
    floor_to_window_start,
    get_snapshot,
    grant_bonus,
    grant_turns,
    refund_turn,
)
from api.infrastructure.adapters.postgres_leads import close_lead_pool
from api.infrastructure.adapters.postgres_quota import PostgresQuotaStore
from api.infrastructure.config.config import get_settings

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "2026-08-24-anon-quota-and-ip-rate-limit.sql"
)
# Test rows are namespaced so teardown stays targeted even against a shared dev db.
_TEST_IDENTITY_PREFIX = "quota-test-"
# Per-project quota scope: every call below pins one project so old single-scope
# assertions keep exercising the same bucket.
_TEST_PROJECT_KEY = "camellia"
# RFC 2544 benchmark range: syntactically valid INET values that cannot collide
# with any real customer address.
_TEST_IP_CLASS_PREFIX = "198.18."


def new_test_identity_key() -> str:
    return f"{_TEST_IDENTITY_PREFIX}{uuid.uuid4()}"


def new_test_ip_address() -> str:
    return f"{_TEST_IP_CLASS_PREFIX}{random.randint(0, 255)}.{random.randint(1, 254)}"


# pytest_asyncio's own decorator is required for async fixtures under strict mode.
@pytest_asyncio.fixture()
async def quota_database():
    """Pool from .env config; skips cleanly when local Postgres is unreachable."""
    try:
        pool = await asyncpg.create_pool(get_settings().pg_dsn, min_size=1, max_size=5, timeout=3)
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(
            "local Postgres unreachable "
            f"({exc.__class__.__name__}); start the docker compose database "
            "to run durable quota tests"
        )
    await pool.execute(_MIGRATION_PATH.read_text(encoding="utf-8"))
    yield pool
    await pool.execute("DELETE FROM anon_quota WHERE identity_key LIKE $1", f"{_TEST_IDENTITY_PREFIX}%")
    await pool.execute("DELETE FROM ip_rate_limit WHERE ip::text LIKE $1", f"{_TEST_IP_CLASS_PREFIX}%")
    # Function-scoped event loops: the cached module pools must not leak into
    # the next test's loop.
    await pool.close()
    await close_lead_pool()


@pytest.mark.asyncio
async def test_consume_turn_race_at_cap_boundary_never_exceeds_allowance(quota_database):
    """20 concurrent consumes at cap=3 yield exactly 3 successes (spec §4 R2)."""
    identity_key = new_test_identity_key()

    outcomes = await asyncio.gather(
        *(consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY) for _ in range(20))
    )

    successes = [outcome for outcome in outcomes if isinstance(outcome, QuotaSnapshot)]
    exhausted = [outcome for outcome in outcomes if isinstance(outcome, QuotaExceeded)]
    assert len(successes) == 3
    assert len(exhausted) == 17
    final_snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert final_snapshot.used_turns == 3
    assert final_snapshot.remaining_turns == 0


@pytest.mark.asyncio
async def test_bonus_grant_is_one_time_idempotent(quota_database):
    """Second grant returns False and never stacks turns (spec §4 R3)."""
    identity_key = new_test_identity_key()

    assert await grant_bonus(identity_key, 5, project_key=_TEST_PROJECT_KEY) is True
    assert await grant_bonus(identity_key, 5, project_key=_TEST_PROJECT_KEY) is False

    snapshot_after_grants = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert snapshot_after_grants.bonus_turns == 5
    assert snapshot_after_grants.bonus_granted is True

    # Lifetime allowance is exactly base + one bonus = 8 turns (spec §4 terminal state).
    lifetime_outcomes = [
        await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY) for _ in range(9)
    ]
    assert sum(isinstance(outcome, QuotaSnapshot) for outcome in lifetime_outcomes) == 8


@pytest.mark.asyncio
async def test_granted_turn_refund_restores_granted_turns(quota_database):
    identity_key = new_test_identity_key()
    assert await grant_turns(
        identity_key, 1, f"grant-{uuid.uuid4()}", project_key=_TEST_PROJECT_KEY
    ) is True

    # Exhaust the base and bonus classes before reserving the granted turn.
    for _ in range(3):
        outcome = await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
        assert isinstance(outcome, QuotaSnapshot)
    reservation = await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert isinstance(reservation, QuotaSnapshot)
    assert reservation.granted_turns == 0
    assert reservation.reservation_id is not None

    refunded = await refund_turn(
        identity_key,
        3,
        project_key=_TEST_PROJECT_KEY,
        reservation_id=reservation.reservation_id,
    )
    assert refunded is not None
    assert refunded.granted_turns == 1
    assert await refund_turn(
        identity_key, 3, project_key=_TEST_PROJECT_KEY, reservation_id=reservation.reservation_id
    ) is None


@pytest.mark.asyncio
async def test_successful_finalize_removes_reservation_without_refunding(quota_database):
    identity_key = new_test_identity_key()
    reservation = await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert isinstance(reservation, QuotaSnapshot)
    assert reservation.reservation_id is not None
    assert await PostgresQuotaStore().finalize_turn_atomically(
        identity_key, project_key=_TEST_PROJECT_KEY, reservation_id=reservation.reservation_id
    ) is True
    assert await PostgresQuotaStore().finalize_turn_atomically(
        identity_key, project_key=_TEST_PROJECT_KEY, reservation_id=reservation.reservation_id
    ) is False
    snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert snapshot.used_turns == 1
    assert snapshot.remaining_turns == 2
    assert await quota_database.fetchval(
        "SELECT count(*) FROM quota_reservations WHERE identity_key = $1", identity_key
    ) == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_refund_restores_once(quota_database):
    identity_key = new_test_identity_key()
    reservation = await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert isinstance(reservation, QuotaSnapshot)
    outcomes = await asyncio.gather(
        *(refund_turn(
            identity_key, 3, project_key=_TEST_PROJECT_KEY,
            reservation_id=reservation.reservation_id,
        ) for _ in range(2))
    )
    assert sum(outcome is not None for outcome in outcomes) == 1
    snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert snapshot.used_turns == 0


@pytest.mark.asyncio
async def test_stale_reservation_is_reconciled_and_refunded(quota_database):
    identity_key = new_test_identity_key()
    reservation = await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert isinstance(reservation, QuotaSnapshot)
    await quota_database.execute(
        "UPDATE quota_reservations SET created_at = now() - interval '2 hours' "
        "WHERE reservation_id = $1::uuid", reservation.reservation_id
    )
    await PostgresQuotaStore().purge_stale_records(3600, 90 * 24 * 3600)
    snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert snapshot.used_turns == 0
    assert await quota_database.fetchval(
        "SELECT count(*) FROM quota_reservations WHERE reservation_id = $1::uuid",
        reservation.reservation_id,
    ) == 0


@pytest.mark.asyncio
async def test_snapshot_remaining_math_follows_spec_formula(quota_database):
    """remaining = cap - used + bonus at every stage of the state machine."""
    identity_key = new_test_identity_key()

    fresh_snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert (fresh_snapshot.used_turns, fresh_snapshot.remaining_turns) == (0, 3)

    await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    mid_snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert mid_snapshot.remaining_turns == 1  # 3 - 2 + 0

    await grant_bonus(identity_key, 5, project_key=_TEST_PROJECT_KEY)
    bonused_snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert bonused_snapshot.remaining_turns == 6  # 3 - 2 + 5

    await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    post_snapshot = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    assert (post_snapshot.used_turns, post_snapshot.remaining_turns) == (3, 5)


class _FixedBucketWindowStore:
    """Deterministic clock for rollover tests with real Postgres counting.

    check_ip_limit derives window_start from the UTC wall clock; near a real
    window boundary the bucket can roll between two calls, making the deny
    assertion flaky. This wrapper substitutes controlled bucket origins while
    delegating the counter upsert to the real adapter, so the SQL semantics
    (per (ip, kind, window_start) counting) stay under test.
    """

    def __init__(self, inner: PostgresQuotaStore, buckets: list[datetime]) -> None:
        self._inner = inner
        self._buckets = buckets
        self.bucket_index = 0

    async def record_ip_window_request(
        self, ip_address: str, kind: str, window_start: datetime
    ) -> int:
        return await self._inner.record_ip_window_request(
            ip_address, kind, self._buckets[self.bucket_index]
        )


@pytest.mark.asyncio
async def test_ip_limit_blocks_at_threshold_then_allows_after_window_rollover(quota_database):
    """Fixed window: budget exhausts, next bucket admits again."""
    ip_address = new_test_ip_address()
    # Injected bucket origins are far from real wall-clock boundaries, so calls
    # land in the intended bucket deterministically; advancing the index models
    # the rollover instead of sleeping across a live boundary.
    base = floor_to_window_start(datetime.now(timezone.utc), 3600)
    store = _FixedBucketWindowStore(
        PostgresQuotaStore(),
        [base + timedelta(hours=offset) for offset in (0, 1)],
    )

    assert await check_ip_limit(ip_address, "mint", 2, 1, storage=store) is True
    assert await check_ip_limit(ip_address, "mint", 2, 1, storage=store) is True
    # Denied attempts keep counting, so probing past the threshold never resets the bucket.
    assert await check_ip_limit(ip_address, "mint", 2, 1, storage=store) is False

    store.bucket_index = 1
    assert await check_ip_limit(ip_address, "mint", 2, 1, storage=store) is True


@pytest.mark.asyncio
async def test_quota_state_survives_pool_restart(quota_database):
    """Persistence is durable across pool teardown/rebuild (process restart analog)."""
    identity_key = new_test_identity_key()
    await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)
    await consume_turn(identity_key, 3, project_key=_TEST_PROJECT_KEY)

    await close_lead_pool()
    snapshot_after_restart = await get_snapshot(identity_key, 3, project_key=_TEST_PROJECT_KEY)

    assert snapshot_after_restart.used_turns == 2
    assert snapshot_after_restart.remaining_turns == 1


@pytest.mark.asyncio
async def test_fcm_register_ip_limit_kind_is_allowed():
    """FCM device registration rate limiting uses its own kind; it must pass
    the allowlist (regression: POST /api/notifications/device-token 500)."""
    recorded: list[tuple[str, str]] = []

    class _RecordingWindowStore:
        async def record_ip_window_request(self, ip_address: str, kind: str, window_start) -> int:
            recorded.append((ip_address, kind))
            return 1

    # 203.0.113.x is RFC 5737 documentation space; the token-like key is a
    # dummy value, not a credential.
    allowed = await check_ip_limit(
        "203.0.113.9",
        "fcm-register",
        5,
        60,
        storage=_RecordingWindowStore(),  # type: ignore[arg-type]
    )
    assert allowed is True
    assert recorded == [("203.0.113.9", "fcm-register")]


@pytest.mark.asyncio
async def test_unknown_ip_limit_kind_fails_closed_before_any_storage_touch():
    """A typo'd kind raises instead of silently opening an unthrottled lane."""
    store_probe = PostgresQuotaStore()
    with pytest.raises(ValueError, match="not allowed"):
        # No database fixture: validation must reject BEFORE reaching storage.
        await check_ip_limit("203.0.113.9", "bogus-kind", 5, 60)
    assert store_probe is not None  # adapter import surface stays healthy


@pytest.mark.asyncio
async def test_quota_allowance_is_scoped_per_project(quota_database):
    """Exhausting one project never touches another project's allowance.

    The composite PK (identity_key, project_key) is the atomic scoping: same
    identity, different project => a fresh allowance, and switching back to the
    exhausted project stays walled.
    """
    identity_key = new_test_identity_key()

    for _ in range(3):
        outcome = await consume_turn(identity_key, 3, project_key="camellia")
        assert isinstance(outcome, QuotaSnapshot)
    blocked_a = await consume_turn(identity_key, 3, project_key="camellia")
    assert isinstance(blocked_a, QuotaExceeded)

    # Project B owns a separate allowance for the same identity.
    fresh_b = await consume_turn(identity_key, 3, project_key="soleil")
    assert isinstance(fresh_b, QuotaSnapshot)
    assert (fresh_b.used_turns, fresh_b.remaining_turns) == (1, 2)

    # Switching back to the exhausted project stays walled (no recycle).
    blocked_a_again = await consume_turn(identity_key, 3, project_key="camellia")
    assert isinstance(blocked_a_again, QuotaExceeded)

    b_snapshot = await get_snapshot(identity_key, 3, project_key="soleil")
    assert (b_snapshot.used_turns, b_snapshot.remaining_turns) == (1, 2)


def test_window_floor_buckets_align_for_every_moment_in_the_bucket():
    """All workers derive identical bucket origins from synchronized UTC clocks."""
    arbitrary_moment = datetime(2026, 8, 24, 12, 34, 56, tzinfo=timezone.utc)
    bucket_start = floor_to_window_start(arbitrary_moment, 300)
    assert bucket_start == datetime(2026, 8, 24, 12, 30, 0, tzinfo=timezone.utc)
    assert floor_to_window_start(bucket_start + timedelta(seconds=299), 300) == bucket_start
    assert floor_to_window_start(bucket_start + timedelta(seconds=300), 300) != bucket_start
