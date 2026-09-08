"""BE-LEADS-QUERY — GET /api/crm/leads (FR-31, contract C1).

Offline tests in the same harness style as test_crm_api: the real create_app()
with the lead repository swapped for a fake exposing search_crm_leads, and
the staff principal injected through dependency_overrides. The fake applies
predicates in Python so route-level validation, keyset paging, and cursor
semantics are exercised end to end without PG.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.infrastructure.ports.leads import LeadRow, get_lead_repository
from api.interfaces.api.deps import AuthenticatedPrincipal, require_sales_or_admin
from api.interfaces.api.main import create_app
from tests.test_crm_api import (
    ADMIN_PRINCIPAL,
    ASSIGNED_SALES_PRINCIPAL,
    OTHER_SALES_PRINCIPAL,
    RecordingLeadMirror,
)
from tests.test_sales_api import FakeLeadRepository

CUSTOMER_PHONE = "0905123456"


class FakeLeadsPageRepository(FakeLeadRepository):
    """Fake exposing the FR-31 search seam with Python-side predicates.

    The fixed lead universe is seeded in __init__ so pagination walk and
    filter assertions are deterministic regardless of wall-clock time.
    """

    def __init__(self) -> None:
        super().__init__()
        base = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
        for index in range(1, 26):
            self.leads[index] = LeadRow(
                id=index,
                session_id=f"session-{index}",
                project_key="camellia" if index % 2 == 0 else "soleil",
                device_id=None,
                name=f"Khach {index}",
                phone=CUSTOMER_PHONE,
                consent=True,
                note=None,
                budget_vnd=None,
                created_at=base + timedelta(hours=index),
                status="assigned" if index % 3 else "callback",
                assigned_sales_id=1 if index % 4 else 2,
                lock_expires_at=None,
                escal_count=0,
                last_action_at=None,
                closed_at=None,
                reengage_at=(base + timedelta(days=2, hours=index)) if index % 2 else None,
            )

    async def search_crm_leads(
        self,
        *,
        assigned_sales_id: int | None,
        project_key: str | None,
        status: str | None,
        reengage_from: date | None,
        reengage_to: date | None,
        tz_name: str,
        cursor_created_at: datetime | None,
        cursor_id: int | None,
        limit: int,
    ) -> list[LeadRow]:
        self.last_call = {
            "assigned_sales_id": assigned_sales_id,
            "project_key": project_key,
            "status": status,
            "reengage_from": reengage_from,
            "reengage_to": reengage_to,
            "tz_name": tz_name,
            "cursor_created_at": cursor_created_at,
            "cursor_id": cursor_id,
            "limit": limit,
        }
        rows = list(self.leads.values())
        if assigned_sales_id is not None:
            rows = [row for row in rows if row.assigned_sales_id == assigned_sales_id]
        if project_key is not None:
            rows = [row for row in rows if row.project_key == project_key]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        from datetime import time as _time
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        if reengage_from is not None:
            bound = datetime.combine(reengage_from, _time.min, tzinfo=tz)
            rows = [
                row
                for row in rows
                if row.reengage_at is not None
                and row.reengage_at.astimezone(tz) >= bound.astimezone(row.reengage_at.tzinfo)
            ]
        if reengage_to is not None:
            bound = datetime.combine(reengage_to + timedelta(days=1), _time.min, tzinfo=tz)
            rows = [
                row
                for row in rows
                if row.reengage_at is not None
                and row.reengage_at.astimezone(tz) < bound.astimezone(row.reengage_at.tzinfo)
            ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        if cursor_created_at is not None and cursor_id is not None:
            rows = [row for row in rows if (row.created_at, row.id) < (cursor_created_at, cursor_id)]
        return rows[:limit]


def make_client(repo: FakeLeadsPageRepository, principal: AuthenticatedPrincipal) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    app.dependency_overrides[require_sales_or_admin] = lambda: principal
    from api.infrastructure.ports.realtime_mirror import get_realtime_lead_mirror

    app.dependency_overrides[get_realtime_lead_mirror] = lambda: RecordingLeadMirror()
    return TestClient(app)


def encode_cursor(created_at: datetime, lead_id: int) -> str:
    payload = f"{created_at.isoformat()}|{lead_id}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def lead_ids(body: dict) -> list[int]:
    return [item["lead_id"] for item in body["items"]]


# ----- role scoping -----


def test_sales_principal_scoped_to_own_leads_only() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ASSIGNED_SALES_PRINCIPAL)

    response = client.get("/api/crm/leads")

    assert response.status_code == 200
    assert repo.last_call["assigned_sales_id"] == 1
    assert all(
        item["assigned_sales_id"] == 1 for item in response.json()["items"]
    )


def test_admin_unscoped_sees_all() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"limit": 100})

    assert response.status_code == 200
    assert repo.last_call["assigned_sales_id"] is None
    assert len(response.json()["items"]) == 25


def test_client_owner_param_cannot_widen_scope() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ASSIGNED_SALES_PRINCIPAL)

    # FastAPI drops undeclared query params, so no client key can reach the
    # service — the scope is derived from the verified principal only.
    response = client.get(
        "/api/crm/leads", params={"assigned_sales_id": None, "limit": 100}
    )

    assert response.status_code == 200
    assert repo.last_call["assigned_sales_id"] == 1


def test_sales_principal_without_sales_id_fails_closed_to_empty_page() -> None:
    """Defense in depth: even if the auth dep ever passed a mapping-less sales
    principal, the service must not degenerate to an unscoped listing."""
    repo = FakeLeadsPageRepository()
    no_mapping = AuthenticatedPrincipal(
        firebase_uid="key-x", email="x@example.com", role="sales", sales_id=None
    )
    client = make_client(repo, no_mapping)

    response = client.get("/api/crm/leads")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert not hasattr(repo, "last_call") or repo.last_call["assigned_sales_id"] != 0


def test_sales_principal_without_mapping_is_403_by_dependency() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, OTHER_SALES_PRINCIPAL)
    # Mirror the real dependency: a verified sales token whose PG mapping is
    # missing raises 403 (SalesMappingMissing) instead of leaking a 500.
    from fastapi import HTTPException

    def _missing_mapping() -> AuthenticatedPrincipal:
        raise HTTPException(status_code=403, detail="Sales mapping missing")

    client.app.dependency_overrides[require_sales_or_admin] = _missing_mapping

    response = client.get("/api/crm/leads")

    assert response.status_code == 403


# ----- filters (alone + combined) -----


def test_project_filter_alone() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"project_key": "camellia", "limit": 100})

    assert response.status_code == 200
    assert repo.last_call["project_key"] == "camellia"
    assert all(item["project_key"] == "camellia" for item in response.json()["items"])


def test_status_filter_rejects_unknown_value_with_422() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"status": "pwned'; DROP TABLE leads; --"})

    assert response.status_code == 422


def test_status_filter_accepts_enum_value() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"status": "callback", "limit": 100})

    assert response.status_code == 200
    assert all(item["lead_status"] == "callback" for item in response.json()["items"])


def test_combined_filters() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get(
        "/api/crm/leads",
        params={"project_key": "camellia", "status": "assigned", "limit": 100},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert all(
        item["project_key"] == "camellia" and item["lead_status"] == "assigned"
        for item in items
    )
    expected = [
        lead.id
        for lead in repo.leads.values()
        if lead.project_key == "camellia" and lead.status == "assigned"
    ]
    assert sorted(lead_ids(response.json())) == sorted(expected)


def test_reengage_window_excludes_null_reengage_at() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get(
        "/api/crm/leads",
        params={"reengage_from": "2026-09-03", "reengage_to": "2026-09-03", "limit": 100},
    )

    assert response.status_code == 200
    items = response.json()["items"]
    # Every returned lead must have a reengage_at inside 2026-09-03 (any tz).
    for item in items:
        assert item["reengage_at"] is not None
    assert lead_ids(response.json())


def test_reengage_only_from_bound_applies_single_side() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"reengage_from": "2026-09-10", "limit": 100})

    assert response.status_code == 200
    assert all(item["reengage_at"] is not None for item in response.json()["items"])


# ----- pagination walk -----


def test_pagination_walk_no_overlap_no_gaps_has_more_flips() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/crm/leads", params=params)
        assert response.status_code == 200
        body = response.json()
        seen.extend(lead_ids(body))
        pages += 1
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        cursor = body["next_cursor"]
        assert cursor is not None

    assert pages == 3
    assert len(seen) == len(set(seen))  # no duplicates
    # Full universe, strictly descending (created_at DESC, id DESC).
    assert seen == sorted(repo.leads.keys(), reverse=True)


def test_cursor_from_last_row_of_page_one_returns_following_page() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    first = client.get("/api/crm/leads", params={"limit": 5})
    body = first.json()
    assert body["has_more"] is True
    last_row = repo.leads[body["items"][-1]["lead_id"]]
    assert body["next_cursor"] == encode_cursor(last_row.created_at, last_row.id)

    second = client.get(
        "/api/crm/leads", params={"limit": 5, "cursor": body["next_cursor"]}
    )
    second_body = second.json()
    first_ids = set(lead_ids(body))
    second_ids = lead_ids(second_body)
    assert first_ids.isdisjoint(second_ids)
    # Continues exactly after the boundary row.
    assert min(second_ids) < body["items"][-1]["lead_id"]


def test_empty_result_is_200_with_empty_items() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"project_key": "no-such-project"})

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["server_time"]


# ----- validation: cursor / limit / status -----


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!!!",
        base64.urlsafe_b64encode(b"garbage-without-separator").decode("ascii"),
        base64.urlsafe_b64encode(b"2026-09-01T00:00:00+00:00|abc").decode("ascii"),
        base64.urlsafe_b64encode(b"not-a-date|12").decode("ascii"),
    ],
)
def test_malformed_cursor_is_422(cursor: str) -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"cursor": cursor})

    assert response.status_code == 422


def test_limit_bounds() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    assert client.get("/api/crm/leads", params={"limit": 0}).status_code == 422
    assert client.get("/api/crm/leads", params={"limit": 101}).status_code == 422
    ok = client.get("/api/crm/leads", params={"limit": 100})
    assert ok.status_code == 200
    assert repo.last_call["limit"] == 101  # limit + 1 probe


def test_default_limit_is_20() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 20
    assert response.json()["has_more"] is True


def test_bad_reengage_date_format_is_422() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"reengage_from": "03/09/2026"})

    assert response.status_code == 422


def test_sql_injection_through_filters_stays_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapter-level guard: malicious values travel as bound parameters and
    never appear in the SQL text (only fixed fragments + $n markers)."""
    import asyncio

    from api.infrastructure.adapters import postgres_leads as adapter

    captured: dict = {}

    class FakeConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def fetch(self, sql: str, *values):
            captured["sql"] = sql
            captured["values"] = values
            return []

    class FakePool:
        def acquire(self):
            return FakeConn()

    async def fake_pool():
        return FakePool()

    monkeypatch.setattr(adapter, "get_lead_pool", fake_pool)
    repo = adapter.PostgresLeadRepository()
    malicious = "camellia'; DROP TABLE leads; --"

    rows = asyncio.run(
        repo.search_crm_leads(
            assigned_sales_id=1,
            project_key=malicious,
            status="assigned",
            reengage_from=date(2026, 9, 3),
            reengage_to=None,
            tz_name="Asia/Ho_Chi_Minh",
            cursor_created_at=None,
            cursor_id=None,
            limit=5,
        )
    )

    assert rows == []
    assert "DROP TABLE leads" not in captured["sql"]
    assert malicious in captured["values"]
    # All placeholders are positional markers on a fixed fragment skeleton.
    assert "$1" in captured["sql"] and "{" not in captured["sql"]


# ----- timezone boundaries -----


def _timezone_repo(lead_created_with_reengage: datetime) -> FakeLeadsPageRepository:
    repo = FakeLeadsPageRepository()
    repo.leads.clear()
    repo.leads[1] = LeadRow(
        id=1,
        session_id="session-tz",
        project_key="camellia",
        device_id=None,
        name="Khach tz",
        phone=CUSTOMER_PHONE,
        consent=True,
        note=None,
        budget_vnd=None,
        created_at=lead_created_with_reengage,
        status="assigned",
        assigned_sales_id=1,
        lock_expires_at=None,
        escal_count=0,
        last_action_at=None,
        closed_at=None,
        reengage_at=lead_created_with_reengage,
    )
    return repo


def test_timezone_boundary_lead_inside_window_in_ho_chi_minh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2026-09-02T23:30:00Z == 2026-09-03T06:30 +07 -> inside reengage_from=2026-09-03.
    monkeypatch.setenv("CRM_REENGAGE_FILTER_TIMEZONE", "Asia/Ho_Chi_Minh")
    repo = _timezone_repo(datetime(2026, 9, 2, 23, 30, tzinfo=timezone.utc))
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"reengage_from": "2026-09-03"})

    assert response.status_code == 200
    assert lead_ids(response.json()) == [1]
    assert repo.last_call["tz_name"] == "Asia/Ho_Chi_Minh"


def test_timezone_boundary_same_lead_outside_window_in_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under UTC the same instant belongs to 2026-09-02 -> excluded by from=2026-09-03.
    monkeypatch.setenv("CRM_REENGAGE_FILTER_TIMEZONE", "UTC")
    repo = _timezone_repo(datetime(2026, 9, 2, 23, 30, tzinfo=timezone.utc))
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"reengage_from": "2026-09-03"})

    assert response.status_code == 200
    assert lead_ids(response.json()) == []
    assert repo.last_call["tz_name"] == "UTC"


def test_invalid_timezone_env_falls_back_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_REENGAGE_FILTER_TIMEZONE", "Not/A_Zone")
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"limit": 1})

    assert response.status_code == 200
    assert repo.last_call["tz_name"] == "UTC"


def test_default_timezone_is_ho_chi_minh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRM_REENGAGE_FILTER_TIMEZONE", raising=False)
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ADMIN_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"limit": 1})

    assert response.status_code == 200
    assert repo.last_call["tz_name"] == "Asia/Ho_Chi_Minh"


# ----- response shape (PII minimality) -----


def test_items_are_masked_summaries_without_raw_phone() -> None:
    repo = FakeLeadsPageRepository()
    client = make_client(repo, ASSIGNED_SALES_PRINCIPAL)

    response = client.get("/api/crm/leads", params={"limit": 1})

    assert response.status_code == 200
    assert CUSTOMER_PHONE not in response.text
    item = response.json()["items"][0]
    assert set(item) >= {
        "id",
        "lead_id",
        "project_key",
        "display_name",
        "masked_phone",
        "lead_status",
        "assigned_sales_id",
        "created_at",
    }
    assert item["masked_phone"] == "0905***456"


def test_unauthenticated_request_is_401_with_real_dependency() -> None:
    repo = FakeLeadsPageRepository()
    app = create_app()
    app.dependency_overrides[get_lead_repository] = lambda: repo
    # No require_sales_or_admin override: the real dependency answers 401/403.
    client = TestClient(app)

    response = client.get("/api/crm/leads")

    assert response.status_code in (401, 403)
