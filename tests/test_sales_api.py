"""Story 6.4: sales API key auth, LRU routing, and customer lead submission."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api.infrastructure.dependencies as dependencies
from api.application.services.lead_mirror_service import compute_customer_id
from api.application.services.lead_service import (
    choose_next_sales,
    create_customer_lead,
    find_recent_lead_within_phone_cooldown,
    get_sales_dashboard,
    handle_lead_action,
    normalize_persisted_datetime,
    validate_phone,
)
from api.infrastructure.ports.firebase_auth import (
    FirebaseAuthTokenError,
    VerifiedFirebaseUser,
)
from api.infrastructure.ports.leads import (
    AssignmentDecision,
    AssignmentLogRow,
    LeadRow,
    SalesRow,
    SalesStats,
    get_lead_repository,
)
from api.interfaces.api.main import create_app


def sales_row(
    sales_id: int,
    *,
    key: str | None = None,
    priority: int = 1,
    last_assigned_at: datetime | None = None,
    firebase_uid: str | None = None,
) -> SalesRow:
    return SalesRow(
        id=sales_id,
        access_key=key or f"key-{sales_id}",
        full_name=f"Sales {sales_id}",
        role=None,
        phone=None,
        is_active=True,
        priority=priority,
        last_seen_at=None,
        last_assigned_at=last_assigned_at,
        firebase_uid=firebase_uid,
    )


def lead_row(lead_id: int, *, assigned_sales_id: int | None = None, status: str = "new") -> LeadRow:
    now = datetime.now()
    return LeadRow(
        id=lead_id,
        session_id="session-1",
        project_key=None,
        device_id=None,
        name="Khách test",
        phone="0905123456",
        consent=True,
        note="Quan tâm căn 2PN",
        budget_vnd=4_000_000_000,
        created_at=now,
        status=status,
        assigned_sales_id=assigned_sales_id,
        lock_expires_at=None,
        escal_count=0,
        last_action_at=None,
        closed_at=None,
    )


class FakeLeadRepository:
    def __init__(self) -> None:
        self.sales = [sales_row(1, priority=10), sales_row(2, priority=5)]
        self.leads: dict[int, LeadRow] = {}
        self.logs: list[AssignmentLogRow] = []
        self.last_seen: list[int] = []
        self.next_lead_id = 1
        self.call_notified: set[int] = set()

    async def stamp_call_started(self, lead_id: int) -> bool:
        # CAS twin: a single winner, and only when the row is already 'called'.
        lead = self.leads.get(lead_id)
        if lead is None or lead.status != "called" or lead_id in self.call_notified:
            return False
        self.call_notified.add(lead_id)
        return True

    async def get_sales_by_key(self, access_key: str) -> SalesRow | None:
        return next((sales for sales in self.sales if sales.access_key == access_key), None)

    async def update_sales_last_seen(self, sales_id: int) -> None:
        self.last_seen.append(sales_id)

    async def list_active_sales(self) -> list[SalesRow]:
        return self.sales

    async def create_lead(self, **kwargs) -> LeadRow:
        lead = lead_row(self.next_lead_id)
        self.next_lead_id += 1
        lead = replace(
            lead,
            session_id=kwargs["session_id"],
            project_key=kwargs.get("project_key"),
            device_id=kwargs.get("device_id"),
            name=kwargs["name"],
            phone=kwargs["phone"],
            consent=kwargs["consent"],
            note=kwargs["note"],
            budget_vnd=kwargs["budget_vnd"],
        )
        self.leads[lead.id] = lead
        return lead

    async def get_active_leads_for_sales(self, sales_id: int, limit: int = 50) -> list[LeadRow]:
        return [
            lead
            for lead in self.leads.values()
            if lead.assigned_sales_id == sales_id and lead.status in ("assigned", "callback")
        ][:limit]

    async def get_lead_by_id(self, lead_id: int) -> LeadRow | None:
        return self.leads.get(lead_id)

    async def update_lead(
        self,
        lead_id: int,
        *,
        status: str,
        assigned_sales_id: int | None = None,
        lock_expires_at=None,
        close: bool = False,
        expected_assigned_sales_id: int | None = None,
    ) -> LeadRow | None:
        lead = self.leads.get(lead_id)
        if lead is None:
            return None
        if (
            expected_assigned_sales_id is not None
            and lead.assigned_sales_id != expected_assigned_sales_id
        ):
            # Former owner: guarded update affects zero rows; no mutation.
            return None
        lead = replace(
            lead,
            status=status,
            assigned_sales_id=(
                lead.assigned_sales_id if assigned_sales_id is None else assigned_sales_id
            ),
            lock_expires_at=(
                lock_expires_at if lock_expires_at is not None else lead.lock_expires_at
            ),
            closed_at=datetime.now() if close else lead.closed_at,
        )
        self.leads[lead_id] = lead
        return lead

    async def assign_lead(
        self,
        lead_id: int,
        *,
        excluded_sales_ids: list[int],
        action: str = "assign",
        note: str | None = None,
        expected_assigned_sales_id: int | None = None,
        prelude_actions: tuple[tuple[str, int | None, str | None], ...] = (),
        preferred_firebase_uid: str | None = None,
    ) -> AssignmentDecision:
        """Deterministic in-memory twin of the atomic Postgres decision.

        Mirrors the production contract exactly: the ownership predicate is
        evaluated against the CURRENT row state, so a former owner whose
        guarded update would affect zero rows gets no prelude log, no
        assignment log and no row change — candidates, mutation and log
        insertion can never diverge because they run against one snapshot.
        """
        lead = self.leads.get(lead_id)
        if lead is None or lead.status in ("booked", "lost", "expired"):
            return AssignmentDecision(lead=None, next_sales=None)
        if (
            expected_assigned_sales_id is not None
            and lead.assigned_sales_id != expected_assigned_sales_id
        ):
            return AssignmentDecision(lead=None, next_sales=None)
        logs_written: list[str] = []
        for pre_action, pre_sales_id, pre_note in prelude_actions:
            row = AssignmentLogRow(
                len(self.logs) + 1, lead_id, pre_sales_id, pre_action, pre_note, datetime.now()
            )
            self.logs.append(row)
            logs_written.append(pre_action)
        tried_ids = await self.get_tried_sales_ids(lead_id)
        candidate = choose_next_sales(
            self.sales,
            list(set(list(excluded_sales_ids) + tried_ids)),
            preferred_firebase_uid=preferred_firebase_uid,
        )
        if candidate is None:
            updated = await self.update_lead(
                lead_id,
                status="expired",
                close=True,
                expected_assigned_sales_id=expected_assigned_sales_id,
            )
            if updated is not None:
                self.logs.append(
                    AssignmentLogRow(
                        len(self.logs) + 1,
                        lead_id,
                        None,
                        "expired",
                        "[ESCALATED-ALL]",
                        datetime.now(),
                    )
                )
                logs_written.append("expired")
            return AssignmentDecision(
                lead=updated,
                next_sales=None,
                logs_written=tuple(logs_written),
            )
        updated = await self.update_lead(
            lead_id,
            status="assigned",
            assigned_sales_id=candidate.id,
            lock_expires_at=datetime.now() + timedelta(minutes=5),
            expected_assigned_sales_id=expected_assigned_sales_id,
        )
        if updated is None:
            return AssignmentDecision(lead=None, next_sales=None)
        self.logs.append(
            AssignmentLogRow(
                len(self.logs) + 1, lead_id, candidate.id, action, note, datetime.now()
            )
        )
        logs_written.append(action)
        return AssignmentDecision(
            lead=updated, next_sales=candidate, logs_written=tuple(logs_written)
        )

    async def add_assignment_log(
        self, lead_id: int, sales_id: int | None, action: str, note: str | None
    ) -> AssignmentLogRow:
        row = AssignmentLogRow(len(self.logs) + 1, lead_id, sales_id, action, note, datetime.now())
        self.logs.append(row)
        return row

    async def get_tried_sales_ids(self, lead_id: int) -> list[int]:
        return [
            log.sales_id for log in self.logs if log.lead_id == lead_id and log.sales_id is not None
        ]

    async def get_sales_stats(self, sales_id: int) -> SalesStats:
        return SalesStats(
            today={
                "assigned": 0,
                "called": 0,
                "heard": 0,
                "booked": 0,
                "no_answer": 0,
                "escalated": 0,
            },
            avg_answer_seconds=None,
            avg_answer_seconds_reason="No assigned-to-call response observations today",
        )

    async def get_sales_by_id(self, sales_id: int) -> SalesRow | None:
        return next((sales for sales in self.sales if sales.id == sales_id), None)

    async def get_sales_for_admin(self, firebase_uid: str) -> SalesRow | None:
        return next((sales for sales in self.sales if sales.firebase_uid == firebase_uid), None)

    async def set_lead_mirror_status(self, lead_id: int, *, mirror_status: str) -> LeadRow | None:
        lead = self.leads.get(lead_id)
        if lead is None:
            return None
        lead = replace(lead, mirror_status=mirror_status)
        self.leads[lead_id] = lead
        return lead

    async def list_stale_mirror_leads(self, *, stale_before, limit: int) -> list[LeadRow]:
        # Fake rows store naive local timestamps; normalize the cutoff the same way.
        cutoff = (
            stale_before.replace(tzinfo=None) if stale_before.tzinfo is not None else stale_before
        )
        stale = [
            lead
            for lead in sorted(self.leads.values(), key=lambda item: item.created_at)
            if lead.mirror_status in ("pending", "failed") and lead.created_at < cutoff
        ]
        return stale[:limit]

    async def get_leads_by_phone(self, phone: str) -> list[LeadRow]:
        return [
            lead
            for lead in sorted(self.leads.values(), key=lambda item: item.created_at, reverse=True)
            if lead.phone == phone
        ]

    async def get_leads_by_customer_id(self, customer_id: str) -> list[LeadRow]:
        # Same HMAC-of-phone derivation the PG adapter recomputes in SQL, so the
        # fake and production agree on what "same customer" means.
        matches = [
            lead for lead in self.leads.values() if compute_customer_id(lead.phone) == customer_id
        ]
        return sorted(
            matches, key=lambda item: normalize_persisted_datetime(item.created_at), reverse=True
        )


def test_choose_next_sales_is_lru_before_priority() -> None:
    older = datetime(2026, 1, 1)
    newer = datetime(2026, 1, 2)
    selected = choose_next_sales(
        [
            sales_row(1, priority=99, last_assigned_at=newer),
            sales_row(2, priority=1, last_assigned_at=older),
        ],
        [],
    )
    assert selected is not None
    assert selected.id == 2


def test_choose_next_sales_prefers_configured_uid_over_lru_repeatedly() -> None:
    """The configured preferred uid wins even when it is the MOST recently
    assigned sales, and keeps winning on every subsequent call — closing the
    acceptance gap where the priority-100 test account only won the FIRST lead
    while it was still unassigned."""
    preferred = sales_row(
        6,
        priority=10,
        last_assigned_at=datetime(2026, 1, 3),
        firebase_uid="uid-preferred",
    )
    others = [
        sales_row(1, priority=99, last_assigned_at=datetime(2026, 1, 1)),
        sales_row(2, priority=1, last_assigned_at=datetime(2026, 1, 2)),
    ]
    for _ in range(2):
        selected = choose_next_sales(
            [*others, preferred], [], preferred_firebase_uid="uid-preferred"
        )
        assert selected is not None
        assert selected.id == 6


def test_choose_next_sales_unset_preferred_uid_preserves_lru_fairness() -> None:
    older = datetime(2026, 1, 1)
    newer = datetime(2026, 1, 2)
    selected = choose_next_sales(
        [
            sales_row(1, priority=99, last_assigned_at=newer),
            sales_row(2, priority=1, last_assigned_at=older),
        ],
        [],
        preferred_firebase_uid=None,
    )
    assert selected is not None
    assert selected.id == 2
    blank = choose_next_sales(
        [
            sales_row(1, priority=99, last_assigned_at=newer),
            sales_row(2, priority=1, last_assigned_at=older),
        ],
        [],
        preferred_firebase_uid="   ",
    )
    assert blank is not None
    assert blank.id == 2


def test_choose_next_sales_unmapped_preferred_uid_falls_back_to_lru() -> None:
    older = datetime(2026, 1, 1)
    newer = datetime(2026, 1, 2)
    selected = choose_next_sales(
        [
            sales_row(1, priority=99, last_assigned_at=newer),
            sales_row(2, priority=1, last_assigned_at=older),
        ],
        [],
        preferred_firebase_uid="uid-not-mapped",
    )
    assert selected is not None
    assert selected.id == 2


def test_choose_next_sales_inactive_preferred_uid_falls_back_to_lru() -> None:
    older = datetime(2026, 1, 1)
    newer = datetime(2026, 1, 2)
    preferred = replace(
        sales_row(6, priority=100, last_assigned_at=older, firebase_uid="uid-preferred"),
        is_active=False,
    )
    selected = choose_next_sales(
        [
            sales_row(1, priority=99, last_assigned_at=newer),
            sales_row(2, priority=1, last_assigned_at=older),
            preferred,
        ],
        [],
        preferred_firebase_uid="uid-preferred",
    )
    assert selected is not None
    assert selected.id == 2


@pytest.mark.asyncio
async def test_preferred_uid_env_routes_every_lead_to_test_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-only wiring: with SALES_PREFERRED_FIREBASE_UID set to a mapped,
    active sales, every new lead is routed to that exact identity repeatedly —
    the realtime manual-verification requirement."""
    monkeypatch.setenv("SALES_PREFERRED_FIREBASE_UID", "uid-test-account")
    from api.infrastructure.config.config import get_settings

    get_settings.cache_clear()
    try:
        repo = FakeLeadRepository()
        repo.sales = [
            sales_row(1, priority=10),
            sales_row(2, priority=5),
            sales_row(6, priority=100, firebase_uid="uid-test-account"),
        ]
        for index in range(3):
            lead = await create_customer_lead(
                repo,
                session_id=f"session-{index}",
                project_key="camellia",
                device_id=None,
                name="Anh Test",
                phone=f"090512345{index}",
                consent=True,
                note=None,
                budget_vnd=None,
            )
            assert lead.status == "assigned"
            assert lead.assigned_sales_id == 6
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_preferred_uid_unset_env_preserves_lru_fairness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the env override the algorithm is unchanged: the LRU-first
    ordering wins over the priority-100 test account once sales carry recent
    assignments — no preference is applied."""
    monkeypatch.delenv("SALES_PREFERRED_FIREBASE_UID", raising=False)
    from api.infrastructure.config.config import get_settings

    get_settings.cache_clear()
    try:
        repo = FakeLeadRepository()
        repo.sales = [
            sales_row(1, priority=10, last_assigned_at=datetime(2026, 1, 2)),
            sales_row(2, priority=5, last_assigned_at=datetime(2026, 1, 1)),
            sales_row(
                6,
                priority=100,
                firebase_uid="uid-test-account",
                last_assigned_at=datetime(2026, 1, 3),
            ),
        ]
        first = await create_customer_lead(
            repo,
            session_id="session-1",
            project_key="camellia",
            device_id=None,
            name="Anh Test",
            phone="0905123451",
            consent=True,
            note=None,
            budget_vnd=None,
        )
        # Sales 2 is the least recently assigned: LRU wins over priority 100.
        assert first.assigned_sales_id == 2
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_preferred_uid_keeps_atomic_predicate_unchanged() -> None:
    """The env override never weakens the atomic owner predicate: a caller
    whose read is stale (lead reparented since the service read) still gets
    conflict=None and zero log rows, exactly as when the preference is unset."""
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name="Anh Test",
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    # The lead moved to sales 2 after the (pretend) caller read it as sales 1.
    repo.leads[lead.id] = replace(repo.leads[lead.id], assigned_sales_id=2)
    before = len(repo.logs)
    decision = await repo.assign_lead(
        lead.id,
        excluded_sales_ids=[1],
        action="assign",
        note="Reassign after no_answer",
        expected_assigned_sales_id=1,
        prelude_actions=(("no_answer", 1, None),),
        preferred_firebase_uid="uid-test-account",
    )
    assert decision.lead is None
    assert decision.next_sales is None
    assert decision.logs_written == ()
    assert len(repo.logs) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created_at",
    [
        pytest.param(datetime.now(timezone.utc) - timedelta(hours=1), id="aware-utc"),
        pytest.param(
            (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(
                timezone(timedelta(hours=7))
            ),
            id="aware-non-utc",
        ),
        pytest.param(
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
            id="legacy-naive-utc",
        ),
    ],
)
async def test_phone_cooldown_normalizes_persisted_timestamps(created_at: datetime) -> None:
    repo = FakeLeadRepository()
    lead = lead_row(1)
    repo.leads[1] = replace(lead, created_at=created_at)

    recent = await find_recent_lead_within_phone_cooldown(
        repo, phone=lead.phone, cooldown_seconds=24 * 60 * 60
    )

    assert recent is not None
    assert recent.id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age, expected",
    [(timedelta(hours=23), True), (timedelta(hours=25), False)],
    ids=["inside-cooldown", "outside-cooldown"],
)
async def test_phone_cooldown_preserves_inside_outside_policy(
    age: timedelta, expected: bool
) -> None:
    repo = FakeLeadRepository()
    lead = lead_row(1)
    repo.leads[1] = replace(lead, created_at=datetime.now(timezone.utc) - age)

    recent = await find_recent_lead_within_phone_cooldown(
        repo, phone=lead.phone, cooldown_seconds=24 * 60 * 60
    )

    assert (recent is not None) is expected


@pytest.mark.asyncio
async def test_phone_cooldown_mixes_legacy_naive_and_aware_offset_rows() -> None:
    repo = FakeLeadRepository()
    lead = lead_row(1)
    now = datetime.now(timezone.utc)
    repo.leads[1] = replace(lead, created_at=(now - timedelta(hours=2)).replace(tzinfo=None))
    repo.leads[2] = replace(
        lead, id=2, created_at=(now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=7)))
    )

    recent = await find_recent_lead_within_phone_cooldown(
        repo, phone=lead.phone, cooldown_seconds=24 * 60 * 60
    )

    assert recent is not None
    assert recent.id == 2


def test_phone_validation_accepts_055_and_rejects_unsupported_prefixes() -> None:
    assert validate_phone("0905123456")
    # 055 is an authoritative Vietnamese mobile prefix (contract: 5[5-9]), so it
    # must be accepted; only truly unsupported prefixes stay rejected (e.g. the
    # landline/legacy 03[0-1], 04x and the 05[0-4] gap the contract leaves out).
    assert validate_phone("0555123456")
    assert not validate_phone("0305123456")
    assert not validate_phone("0495123456")
    assert not validate_phone("0505123456")


@pytest.mark.asyncio
async def test_sales_dashboard_explains_missing_answer_metric() -> None:
    dashboard = await get_sales_dashboard(FakeLeadRepository(), sales_row(1))
    assert dashboard["stats"]["avg_answer_seconds"] is None
    assert dashboard["stats"]["avg_answer_seconds_reason"]


@pytest.mark.asyncio
async def test_submit_lead_assigns_highest_priority_when_all_unassigned() -> None:
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name="Anh Test",
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    assert lead.status == "assigned"
    assert lead.assigned_sales_id == 1
    assert repo.logs[-1].action == "assign"


@pytest.mark.asyncio
async def test_submit_lead_selects_priority_100_test_account_first_and_keys_mirror_by_uid() -> None:
    """Issue 8 acceptance: the provisioned authorized test account (priority 100)
    is the highest-priority active identity, so the FIRST lead (all sales
    unassigned) is routed to it; the mirror must carry its Firebase uid."""
    repo = FakeLeadRepository()
    repo.sales = [
        sales_row(1, priority=10),
        sales_row(2, priority=5),
        sales_row(6, priority=100, firebase_uid="uid-test-account"),
    ]
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name="Anh Test",
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    assert lead.status == "assigned"
    assert lead.assigned_sales_id == 6
    assert repo.logs[-1].action == "assign"
    selected = next(s for s in repo.sales if s.id == lead.assigned_sales_id)
    assert selected.firebase_uid == "uid-test-account"
    # The mirror writer maps the SAME uid (mirror test pins the document field;
    # here we pin that the routed owner is the mapped identity).
    from api.application.services.lead_mirror_service import build_lead_mirror_document

    document = build_lead_mirror_document(lead, sales=selected)
    assert document.assigned_sales_firebase_uid == "uid-test-account"


@pytest.mark.asyncio
async def test_legacy_fallback_no_answer_guards_before_logging() -> None:
    class LegacyRepository(FakeLeadRepository):
        assign_lead = None

        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []
            self.return_stale_snapshot = False

        async def get_lead_by_id(self, lead_id: int) -> LeadRow | None:
            lead = await super().get_lead_by_id(lead_id)
            if self.return_stale_snapshot and lead is not None:
                return replace(lead, assigned_sales_id=1, status="assigned")
            return lead

        async def update_lead(
            self,
            lead_id: int,
            *,
            status: str,
            assigned_sales_id: int | None = None,
            lock_expires_at=None,
            close: bool = False,
            expected_assigned_sales_id: int | None = None,
        ) -> LeadRow | None:
            self.events.append("update")
            return await super().update_lead(
                lead_id,
                status=status,
                assigned_sales_id=assigned_sales_id,
                lock_expires_at=lock_expires_at,
                close=close,
                expected_assigned_sales_id=expected_assigned_sales_id,
            )

        async def add_assignment_log(self, *args, **kwargs) -> AssignmentLogRow:
            self.events.append("log")
            return await super().add_assignment_log(*args, **kwargs)

    repo = LegacyRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    assert repo.events == ["update", "log"]

    repo.leads[lead.id] = replace(repo.leads[lead.id], assigned_sales_id=2, status="assigned")
    log_count = len(repo.logs)
    repo.events.clear()
    repo.return_stale_snapshot = True
    result = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")

    assert result is None
    assert len(repo.logs) == log_count
    assert repo.events == ["update"]
    assert repo.leads[lead.id].assigned_sales_id == 2


@pytest.mark.asyncio
async def test_no_answer_reassigns_to_next_sales_immediately() -> None:
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    updated = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert updated is not None
    assert updated.status == "assigned"
    assert updated.assigned_sales_id == 2
    assert [entry.action for entry in repo.logs][-2:] == ["no_answer", "assign"]


@pytest.mark.asyncio
async def test_former_owner_no_answer_produces_no_log_after_reassignment() -> None:
    """Atomicity pin for the Reviewer race: the no_answer log, the ownership
    predicate and the candidate selection commit as ONE decision. A former
    owner (lead already reparented) affects zero rows and must produce NO log."""
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    first_ok = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert first_ok is not None and first_ok.assigned_sales_id == 2
    log_count = len(repo.logs)

    # Sales 1 no longer owns the lead: any guarded write affects zero rows.
    stale_owner_attempt = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert stale_owner_attempt is None
    # No extra no_answer / assign log materialized for the failed predicate.
    assert len(repo.logs) == log_count
    assert repo.leads[1].assigned_sales_id == 2
    assert [entry.action for entry in repo.logs][-2:] == ["no_answer", "assign"]


@pytest.mark.asyncio
async def test_sales_service_returns_409_when_lead_reassigned_underneath() -> None:
    """HTTP 409 preserved: once an admin/other owner moved the lead, the former
    owner's no_answer fails WITHOUT mutating row or logs (route maps None
    handle_lead_action to 409)."""
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    # Sales 1 submits, but the lead is reassigned to sales 2 underneath.
    repo.leads[lead.id] = replace(repo.leads[lead.id], assigned_sales_id=2, status="assigned")
    log_count = len(repo.logs)
    result = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert result is None  # route raises HTTPException 409
    assert len(repo.logs) == log_count
    assert repo.leads[lead.id].assigned_sales_id == 2


@pytest.mark.asyncio
async def test_no_answer_exhausted_candidates_expires_with_prelude_log() -> None:
    repo = FakeLeadRepository()
    repo.sales = [sales_row(1, priority=5)]
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    updated = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert updated is not None
    assert updated.status == "expired"
    assert updated.closed_at is not None
    assert [entry.action for entry in repo.logs][-2:] == ["no_answer", "expired"]


@pytest.mark.asyncio
async def test_atomic_reassignment_respects_tried_sales_and_lru_order() -> None:
    """Candidates exclude every tried sales (never hand back to the previous
    owner) and order by LRU first, priority only as the tiebreak."""
    repo = FakeLeadRepository()
    repo.sales = [
        sales_row(1, priority=10, last_assigned_at=datetime(2026, 1, 1)),
        sales_row(2, priority=1, last_assigned_at=datetime(2026, 1, 2)),
        sales_row(3, priority=99, last_assigned_at=datetime(2026, 1, 3)),
    ]
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    assert lead.assigned_sales_id == 1  # least recently assigned
    updated = await handle_lead_action(repo, repo.sales[0], lead.id, "no_answer")
    assert updated is not None
    # Sales 1 is excluded + tried; sales 2 (older than sales 3) wins.
    assert updated.assigned_sales_id == 2
    assert [entry.action for entry in repo.logs][-2:] == ["no_answer", "assign"]
    # Tried-sales monotonicity: sales 2 cannot hand the lead back to sales 1.
    second = await handle_lead_action(repo, repo.sales[1], lead.id, "no_answer")
    assert second is not None
    assert second.assigned_sales_id == 3


@pytest.mark.asyncio
async def test_repository_assign_predicate_aborts_before_any_log() -> None:
    """Repository-level atomic contract: assign_lead evaluates the predicate
    against the current row INSIDE the decision. A caller whose read is stale
    (lead reparented since the service read) gets conflict=None and zero
    log rows — candidate selection and log insertion never run."""
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    # The lead moved to sales 2 after the (pretend) caller read it as sales 1.
    repo.leads[lead.id] = replace(repo.leads[lead.id], assigned_sales_id=2)
    before = len(repo.logs)
    decision = await repo.assign_lead(
        lead.id,
        excluded_sales_ids=[1],
        action="assign",
        note="Reassign after no_answer",
        expected_assigned_sales_id=1,
        prelude_actions=(("no_answer", 1, None),),
    )
    assert decision.lead is None
    assert decision.next_sales is None
    assert decision.logs_written == ()
    assert len(repo.logs) == before  # no stale no_answer or assign log


@pytest.mark.asyncio
async def test_repository_assign_writes_prelude_and_assign_together() -> None:
    """Repository-level atomic contract: prelude (no_answer) + assign logs
    materialize together or not at all, and tried-sales filtering reflects the
    prelude immediately (the previous owner is never a candidate)."""
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    decision = await repo.assign_lead(
        lead.id,
        excluded_sales_ids=[1],
        action="assign",
        note="Reassign after no_answer",
        expected_assigned_sales_id=1,
        prelude_actions=(("no_answer", 1, None),),
    )
    assert decision.lead is not None
    assert decision.logs_written == ("no_answer", "assign")
    assert decision.next_sales is not None
    assert decision.next_sales.id == 2
    assert [entry.action for entry in repo.logs][-2:] == ["no_answer", "assign"]


@pytest.mark.asyncio
async def test_called_action_still_disallowed_after_loss() -> None:
    repo = FakeLeadRepository()
    lead = await create_customer_lead(
        repo,
        session_id="session-1",
        project_key="camellia",
        device_id=None,
        name=None,
        phone="0905123456",
        consent=True,
        note=None,
        budget_vnd=None,
    )
    repo.leads[lead.id] = replace(repo.leads[lead.id], status="lost", closed_at=datetime.now())
    before = len(repo.logs)
    assert await handle_lead_action(repo, repo.sales[0], lead.id, "called") is None
    assert len(repo.logs) == before


@pytest.mark.asyncio
async def test_called_action_defers_fcm_push_off_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QC defect pin: the 'called' action queues the FCM push as a background
    task instead of awaiting it inline, so a slow send cannot hold the staff
    response. The handler returns with the sender untouched; only draining the
    queued background work delivers the (unchanged) recipient and payload."""
    from fastapi import BackgroundTasks

    from api.interfaces.api.sales import LeadActionRequest, lead_action

    repo = FakeLeadRepository()
    repo.leads[9] = lead_row(9, assigned_sales_id=1, status="assigned")

    dispatched: list[dict] = []

    class _RecordingFcmService:
        async def notify_sales(self, **kwargs) -> None:
            dispatched.append(kwargs)

        async def resolve_identity_tokens(self, **kwargs) -> list[str]:
            return []

    monkeypatch.setattr(
        dependencies, "get_fcm_notification_service", lambda: _RecordingFcmService()
    )

    background = BackgroundTasks()
    response = await lead_action(
        lead_id=9,
        payload=LeadActionRequest(action="called"),
        background_tasks=background,
        sales=repo.sales[0],
        repo=repo,
        mirror=None,
    )

    assert response.ok is True
    assert response.lead_status == "called"
    # Queued, not awaited: nothing dispatched when the handler returned, so the
    # action response never blocks on the FCM round-trip.
    assert dispatched == []
    # Task 1 = staff-facing sales push, task 2 = customer once-guard dispatch.
    assert len(background.tasks) == 2
    # Draining the background work delivers the same recipient + payload for
    # the staff push...
    await background()
    assert dispatched[0]["sales_id"] == 1
    assert dispatched[0]["data"] == {"type": "sales_call_started", "lead_id": "9"}
    # ...and the customer once-guard claimed the stamp for lead 9 (the fake
    # customer service resolved zero identity tokens, so no extra send).
    assert repo.call_notified == {9}


def test_sales_key_auth_and_customer_submit_http(monkeypatch) -> None:
    monkeypatch.setenv("SALES_LEGACY_KEY_AUTH_ENABLED", "true")
    from api.infrastructure.config.config import get_settings

    get_settings.cache_clear()
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    client = TestClient(app)

    unauthorized = client.get("/api/sales/leads")
    assert unauthorized.status_code == 401
    invalid = client.get("/api/sales/leads", headers={"X-Sales-Key": "not-a-key"})
    assert invalid.status_code == 401

    submitted = client.post(
        "/api/lead",
        json={
            "project_key": "camellia",
            "session_id": "session-http",
            "phone": "0905 123 456",
            "consent": True,
        },
    )
    assert submitted.status_code == 201
    assert submitted.json()["will_call_within_minutes"] == 5

    board = client.get("/api/sales/leads", headers={"X-Sales-Key": "key-1"})
    assert board.status_code == 200
    # phone is masked to protect PII (mask_phone), not the raw number
    assert board.json()["leads"][0]["phone"] == "0905***456"
    # BE-CRM: the assigned lead list carries the opaque customer id so the
    # board can address the reveal route without ever shipping the raw phone.
    assert board.json()["leads"][0]["customer_id"] == compute_customer_id("0905123456")
    assert "0905123456" not in board.text
    assert repo.last_seen == [1]


@pytest.mark.parametrize(
    ("token", "user", "sales", "expected_status"),
    [
        ("bad", FirebaseAuthTokenError("invalid"), None, 401),
        (
            "viewer",
            VerifiedFirebaseUser("uid-viewer", None, True, "viewer", None, None, None),
            None,
            403,
        ),
        (
            "unmapped",
            VerifiedFirebaseUser("uid-missing", None, True, "sales", None, None, None),
            None,
            404,
        ),
        (
            "inactive",
            VerifiedFirebaseUser("uid-inactive", None, True, "sales", None, None, None),
            sales_row(3, firebase_uid="uid-inactive"),
            403,
        ),
    ],
)
def test_firebase_sales_auth_ladder(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    user: VerifiedFirebaseUser | FirebaseAuthTokenError,
    sales: SalesRow | None,
    expected_status: int,
) -> None:
    class Verifier:
        async def verify_id_token(self, id_token: str):
            if isinstance(user, FirebaseAuthTokenError):
                raise user
            return user

    repo = FakeLeadRepository()
    if sales is not None:
        repo.sales = [replace(sales, is_active=False)]
    monkeypatch.setattr(dependencies, "get_firebase_auth_verifier", lambda: Verifier())
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    response = TestClient(app).get("/api/sales/leads", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == expected_status


def test_customer_submit_rejects_missing_consent_and_invalid_phone() -> None:
    repo = FakeLeadRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    client = TestClient(app)

    assert (
        client.post(
            "/api/lead",
            json={
                "project_key": "camellia",
                "phone": "0905123456",
                "consent": False,
            },
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/lead",
            json={"project_key": "camellia", "phone": "123", "consent": True},
        ).status_code
        == 422
    )
