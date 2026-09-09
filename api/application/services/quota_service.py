"""Durable anonymous quota + per-IP rate limiting (secure wave Issue 1B).

Postgres is the system of record for anonymous turn quota (spec §4/§6); the
in-memory LRU conv_state stays hot-path-only conversation context and never
decides allowance. This module owns the typed contract and the business
arithmetic; the SQL lives in the PostgresQuotaStore adapter behind the
QuotaRecordStore protocol, so an alternate backend swaps one adapter.

    Allowance rule (spec §4): a turn may be consumed iff
    used_turns < effective_cap + bonus_turns, i.e. remaining_turns always equals
    max(0, effective_cap - used_turns + bonus_turns). The base cap arrives from
    config per call: anonymous=10, registered customer=5, sales=10; admin
    bypasses this service. The granted anonymous bonus lives in the row, which
    keeps the check self-contained inside one atomic statement (spec §4 R2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol

AllowanceClass = Literal["base", "bonus", "granted", "unlimited"]


logger = logging.getLogger("api.services.quota_service")

# Spec §6 fixes the rate-limit vocabulary; unknown kinds fail closed before any
# storage touch so a typo can never silently open an unthrottled lane.
ALLOWED_IP_LIMIT_KINDS = frozenset({"query", "mint", "lead", "fcm-register"})


@dataclass(frozen=True)
class QuotaRecord:
    """Raw persisted state of one anonymous identity."""

    used_turns: int
    bonus_turns: int
    bonus_granted: bool
    granted_turns: int = 0
    reservation_id: str | None = None
    allowance_class: AllowanceClass | None = None


@dataclass(frozen=True)
class QuotaSnapshot:
    """API-facing quota view (spec §5.1 shape minus auth flags owned by callers)."""

    used_turns: int
    remaining_turns: int
    cap: int
    bonus_turns: int
    granted_turns: int
    bonus_granted: bool
    reservation_id: str | None = None
    allowance_class: AllowanceClass | None = None


@dataclass(frozen=True)
class QuotaExceeded:
    """Exhaustion result carrying the terminal snapshot for the 429 body (§5.2)."""

    final_snapshot: QuotaSnapshot


def build_quota_snapshot(
    *,
    used_turns: int,
    bonus_turns: int,
    bonus_granted: bool,
    effective_cap: int,
    granted_turns: int = 0,
) -> QuotaSnapshot:
    """Apply the spec formula remaining = cap - used + bonus, floored at zero.

    The floor only matters when config shrinks below already-spent usage;
    reporting a negative allowance would poison API payloads.
    """
    remaining_turns = max(0, effective_cap - used_turns + bonus_turns + granted_turns)
    return QuotaSnapshot(
        used_turns=used_turns,
        remaining_turns=remaining_turns,
        cap=effective_cap,
        bonus_turns=bonus_turns,
        granted_turns=granted_turns,
        bonus_granted=bonus_granted,
    )


def floor_to_window_start(moment: datetime, window_seconds: int) -> datetime:
    """Map any instant onto its fixed-window bucket origin in UTC.

    Every worker derives identical buckets from synchronized UTC clocks, so no
    shared coordination is needed for multi-worker correctness.
    """
    epoch_seconds = int(moment.timestamp())
    bucket_origin = epoch_seconds - (epoch_seconds % window_seconds)
    return datetime.fromtimestamp(bucket_origin, tz=timezone.utc)


class QuotaRecordStore(Protocol):
    """Storage primitives; every write must be one atomic statement.

    Quota rows are scoped per (identity_key, project_key) so each project
    carries its own allowance (spec §4 per-project quota).
    """

    async def consume_turn_atomically(
        self, identity_key: str, effective_cap: int, *, project_key: str
    ) -> QuotaRecord | None: ...

    async def refund_turn_atomically(
        self,
        identity_key: str,
        *,
        project_key: str,
        reservation_id: str | None = None,
    ) -> QuotaRecord | None: ...

    async def finalize_turn_atomically(
        self, identity_key: str, *, project_key: str, reservation_id: str
    ) -> bool: ...

    async def grant_one_time_bonus(
        self, identity_key: str, bonus_turn_count: int, *, project_key: str = ""
    ) -> bool: ...

    async def grant_turns_atomically(
        self, identity_key: str, turn_count: int, request_id: str, *, project_key: str
    ) -> bool: ...

    async def link_identity_atomically(self, anon_identity_key: str, firebase_uid: str) -> bool: ...

    async def fetch_quota_record(
        self, identity_key: str, *, project_key: str
    ) -> QuotaRecord | None: ...

    async def record_ip_window_request(
        self, ip_address: str, kind: str, window_start: datetime
    ) -> int: ...

    async def purge_stale_records(
        self, ip_window_retention_seconds: int, identity_retention_seconds: int
    ) -> tuple[int, int]: ...


def _resolve_storage(storage: QuotaRecordStore | None) -> QuotaRecordStore:
    # Late binding keeps this application module free of infrastructure imports;
    # tests inject fakes here instead of patching adapter internals.
    if storage is not None:
        return storage
    from api.infrastructure.adapters.postgres_quota import PostgresQuotaStore

    return PostgresQuotaStore()


async def consume_turn(
    identity_key: str,
    effective_cap: int,
    *,
    project_key: str,
    storage: QuotaRecordStore | None = None,
) -> QuotaSnapshot | QuotaExceeded:
    """Consume one anonymous turn; never lets concurrent requests pass the cap.

    The gate is a single atomic conditional upsert (spec §4 R2): the increment
    happens only while used_turns < effective_cap + bonus_turns, evaluated by
    Postgres under the row lock. The reservation is taken BEFORE the pipeline
    starts; a failed pipeline releases it via refund_turn (spec §4 R1). The
    allowance is scoped per project (spec §4 per-project quota), so switching
    projects never resets an exhausted allowance.
    """
    store = _resolve_storage(storage)
    if effective_cap < 1:
        # Degenerate config: nothing may ever be consumed, and the insert arm of
        # the upsert must never materialize a used=1 row past a zero cap.
        return QuotaExceeded(
            final_snapshot=build_quota_snapshot(
                used_turns=0, bonus_turns=0, bonus_granted=False, effective_cap=effective_cap
            )
        )
    consumed_record = await store.consume_turn_atomically(
        identity_key, effective_cap, project_key=project_key
    )
    if consumed_record is not None:
        snapshot = build_quota_snapshot(
            used_turns=consumed_record.used_turns,
            bonus_turns=consumed_record.bonus_turns,
            granted_turns=consumed_record.granted_turns,
            bonus_granted=consumed_record.bonus_granted,
            effective_cap=effective_cap,
        )
        return QuotaSnapshot(
            **{**snapshot.__dict__, "reservation_id": consumed_record.reservation_id,
               "allowance_class": consumed_record.allowance_class}
        )
    # Exhausted path: read the row purely for reporting; the atomic gate above
    # already decided the outcome, so a racing read cannot over-consume.
    current_record = await store.fetch_quota_record(identity_key, project_key=project_key)
    if current_record is None:
        logger.warning("quota row vanished after failed consume for identity %s", identity_key[:8])
        current_record = QuotaRecord(
            used_turns=effective_cap, bonus_turns=0, granted_turns=0, bonus_granted=False
        )
    return QuotaExceeded(
        final_snapshot=build_quota_snapshot(
            used_turns=current_record.used_turns,
            bonus_turns=current_record.bonus_turns,
            granted_turns=current_record.granted_turns,
            bonus_granted=current_record.bonus_granted,
            effective_cap=effective_cap,
        )
    )


async def finalize_turn(
    identity_key: str,
    *,
    project_key: str,
    reservation_id: str,
    storage: QuotaRecordStore | None = None,
) -> bool:
    """Delete a successful reservation without changing quota counters."""
    return await _resolve_storage(storage).finalize_turn_atomically(
        identity_key, project_key=project_key, reservation_id=reservation_id
    )


async def refund_turn(
    identity_key: str,
    effective_cap: int,
    *,
    project_key: str,
    reservation_id: str | None = None,
    storage: QuotaRecordStore | None = None,
) -> QuotaSnapshot | None:
    """Release one reserved turn after a failed pipeline (spec §4 R1).

    The reservation (consume_turn) happens BEFORE the pipeline runs, so a
    failed pipeline must give the slot back. The decrement is one atomic
    statement guarded by ``used_turns > 0``: a double refund (retried error
    path) is a no-op instead of pushing usage below the true consumed state.
    Returns None when there was nothing to refund (unknown identity or already
    at zero).
    """
    store = _resolve_storage(storage)
    if reservation_id is None:
        refunded_record = await store.refund_turn_atomically(
            identity_key, project_key=project_key
        )
    else:
        refunded_record = await store.refund_turn_atomically(
            identity_key, project_key=project_key, reservation_id=reservation_id
        )
    if refunded_record is None:
        return None
    snapshot = build_quota_snapshot(
        used_turns=refunded_record.used_turns,
        bonus_turns=refunded_record.bonus_turns,
        granted_turns=refunded_record.granted_turns,
        bonus_granted=refunded_record.bonus_granted,
        effective_cap=effective_cap,
    )
    return QuotaSnapshot(
        **{**snapshot.__dict__, "reservation_id": refunded_record.reservation_id,
           "allowance_class": refunded_record.allowance_class}
    )


async def grant_bonus(
    identity_key: str,
    bonus_turn_count: int,
    *,
    project_key: str = "",
    storage: QuotaRecordStore | None = None,
) -> bool:
    """Grant the one-time post-lead bonus; returns False when already granted.

    Idempotency rides the same atomic upsert as consumption (spec §4 R3): the
    flag flip happens once under the row lock, so double lead submission or a
    race between two workers still yields exactly one grant. The bonus row is
    scoped per project when a project_key is supplied; the legacy lead path
    (lead_service.grant_post_lead_bonus_turns) does not thread it yet, so the
    positional call is kept for stores that predate the project-scoped key.
    """
    if bonus_turn_count < 1:
        raise ValueError("bonus_turn_count must be positive")
    store = _resolve_storage(storage)
    if project_key:
        return await store.grant_one_time_bonus(
            identity_key, bonus_turn_count, project_key=project_key
        )
    return await store.grant_one_time_bonus(identity_key, bonus_turn_count)


async def link_identity(
    anon_identity_key: str,
    firebase_uid: str,
    *,
    storage: QuotaRecordStore | None = None,
) -> bool:
    """Link an anonymous identity without changing its quota state."""
    if not anon_identity_key.strip() or not firebase_uid.strip():
        raise ValueError("identity keys are required")
    return await _resolve_storage(storage).link_identity_atomically(anon_identity_key, firebase_uid)


async def grant_turns(
    identity_key: str,
    turn_count: int,
    request_id: str,
    *,
    project_key: str,
    storage: QuotaRecordStore | None = None,
) -> bool:
    """Grant admin turns exactly once for a request id."""
    if turn_count < 1 or not request_id.strip():
        raise ValueError("turn_count and request_id are required")
    return await _resolve_storage(storage).grant_turns_atomically(
        identity_key, turn_count, request_id, project_key=project_key
    )


async def get_snapshot(
    identity_key: str,
    effective_cap: int,
    *,
    project_key: str,
    storage: QuotaRecordStore | None = None,
) -> QuotaSnapshot:
    """Read-only view; an unseen identity reports its full untouched allowance."""
    record = await _resolve_storage(storage).fetch_quota_record(
        identity_key, project_key=project_key
    )
    if record is None:
        return build_quota_snapshot(
            used_turns=0, bonus_turns=0, bonus_granted=False, effective_cap=effective_cap
        )
    return build_quota_snapshot(
        used_turns=record.used_turns,
        bonus_turns=record.bonus_turns,
        granted_turns=record.granted_turns,
        bonus_granted=record.bonus_granted,
        effective_cap=effective_cap,
    )


async def check_ip_limit(
    ip_address: str,
    kind: str,
    max_requests_per_window: int,
    window_seconds: int,
    *,
    storage: QuotaRecordStore | None = None,
) -> bool:
    """Fixed-window per-IP throttle; True means ALLOWED within the budget.

    Denied attempts still count toward the bucket: letting rejected probes go
    uncounted would let a client hammer the endpoint for free right at the
    threshold. Buckets are derived from UTC wall clock, so all workers agree.
    """
    if kind not in ALLOWED_IP_LIMIT_KINDS:
        raise ValueError(f"ip limit kind not allowed: {kind!r}")
    if window_seconds < 1 or max_requests_per_window < 1:
        raise ValueError("window_seconds and max_requests_per_window must be positive")
    window_start = floor_to_window_start(datetime.now(timezone.utc), window_seconds)
    window_counter = await _resolve_storage(storage).record_ip_window_request(
        ip_address, kind, window_start
    )
    return window_counter <= max_requests_per_window


async def purge_expired_quota_records(
    *,
    ip_window_retention_seconds: int = 7 * 24 * 3600,
    identity_retention_seconds: int = 90 * 24 * 3600,
    storage: QuotaRecordStore | None = None,
) -> tuple[int, int]:
    """TTL sweep mirroring the reconciliation-loop style; returns deleted counts.

    Retentions deliberately exceed the 90-day anon token freshness so live
    identities are never evicted while their tokens can still verify.
    """
    return await _resolve_storage(storage).purge_stale_records(
        ip_window_retention_seconds, identity_retention_seconds
    )
