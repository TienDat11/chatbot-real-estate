from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from api.application.ports.sales_provisioning import (
    SalesProvisioningError,
    SalesProvisioningResult,
)
from api.infrastructure.ports.leads import SalesRow
from api.interfaces.api import admin_routes
from api.interfaces.api.deps import AuthenticatedPrincipal

_NOW = datetime.now(timezone.utc)


def _row(*, active: bool = True) -> SalesRow:
    return SalesRow(
        id=7,
        access_key="legacy-key",
        full_name="Existing Sales",
        role="sales",
        phone=None,
        is_active=active,
        priority=1,
        last_seen_at=None,
        last_assigned_at=None,
        firebase_uid="uid-1",
    )


class FakeRepo:
    def __init__(self, existing: SalesRow | None = None, create_error: Exception | None = None):
        self.existing = existing
        self.create_error = create_error
        self.create_calls = 0

    async def get_sales_for_admin(self, firebase_uid: str):
        return self.existing

    async def create_sales_mapping(self, **kwargs):
        self.create_calls += 1
        if self.create_error:
            raise self.create_error
        return _row()


class FakeProvisioner:
    def __init__(self, result=None, provision_error=None, revoke_error=None):
        self.result = result
        self.provision_error = provision_error
        self.revoke_error = revoke_error
        self.provision_calls = 0
        self.revoke_calls = 0

    async def provision(self, **kwargs):
        self.provision_calls += 1
        if self.provision_error:
            raise self.provision_error
        return self.result

    async def revoke(self, **kwargs):
        self.revoke_calls += 1
        if self.revoke_error:
            raise self.revoke_error


class FakeAudit:
    pass


class FakeConnection:
    def __init__(self, row=None, exists=True):
        self.row = row
        self.exists = exists
        self.queries = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        return 1 if self.exists else None


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


@pytest.fixture
def principal():
    return AuthenticatedPrincipal(
        firebase_uid="admin-1", email="admin@example.test", role="admin", sales_id=None
    )


@pytest.mark.asyncio
async def test_bind_sales_uid_by_id_records_reconciliation_audit(monkeypatch, principal):
    row = {
        "id": 7,
        "access_key": "legacy-key",
        "full_name": "Existing Sales",
        "role": "sales",
        "phone": None,
        "is_active": True,
        "priority": 1,
        "last_seen_at": None,
        "firebase_uid": "uid-bound",
        "last_assigned_at": None,
    }
    connection = FakeConnection(row=row)

    async def get_pool():
        return FakePool(connection)

    monkeypatch.setattr(admin_routes, "get_lead_pool", get_pool)
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)
    response = await admin_routes.bind_admin_sales_firebase_uid(
        7, admin_routes.AdminSalesBindRequest(firebase_uuid="uid-bound"), principal, FakeAudit()
    )
    assert response.firebase_uid == "uid-bound"
    assert audit[0]["action"] == admin_routes.STAFF_AUDIT_ACTION_SALES_RECONCILIATION
    assert "$2" in connection.queries[0][0]


@pytest.mark.asyncio
async def test_bind_sales_uid_unknown_id_returns_404(monkeypatch, principal):
    connection = FakeConnection(row=None, exists=False)

    async def get_pool():
        return FakePool(connection)

    monkeypatch.setattr(admin_routes, "get_lead_pool", get_pool)
    with pytest.raises(HTTPException) as error:
        await admin_routes.bind_admin_sales_firebase_uid(
            999,
            admin_routes.AdminSalesBindRequest(firebase_uuid="uid-bound"),
            principal,
            FakeAudit(),
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_bind_sales_uid_already_bound_returns_409(monkeypatch, principal):
    connection = FakeConnection(row=None, exists=True)

    async def get_pool():
        return FakePool(connection)

    monkeypatch.setattr(admin_routes, "get_lead_pool", get_pool)
    with pytest.raises(HTTPException) as error:
        await admin_routes.bind_admin_sales_firebase_uid(
            7, admin_routes.AdminSalesBindRequest(firebase_uuid="uid-bound"), principal, FakeAudit()
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_pg_failure_revokes_new_external_access_and_returns_503(monkeypatch, principal):
    repo = FakeRepo(create_error=RuntimeError("pg down"))
    provisioner = FakeProvisioner(result=SalesProvisioningResult())
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    with pytest.raises(HTTPException) as error:
        await admin_routes.create_admin_sales(
            admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Sales"),
            principal,
            repo,
            provisioner,
            FakeAudit(),
        )

    assert error.value.status_code == 503
    assert provisioner.revoke_calls == 1
    assert audit[0]["detail"]["outcome"] == "compensated"


@pytest.mark.asyncio
async def test_inactive_existing_pg_failure_compensates_firebase(monkeypatch, principal):
    repo = FakeRepo(existing=_row(active=False), create_error=RuntimeError("pg down"))
    provisioner = FakeProvisioner(result=SalesProvisioningResult())
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    with pytest.raises(HTTPException) as error:
        await admin_routes.create_admin_sales(
            admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Sales"),
            principal,
            repo,
            provisioner,
            FakeAudit(),
        )

    assert error.value.__cause__.args == ("pg down",)
    assert provisioner.revoke_calls == 1
    assert audit[0]["detail"] == {
        "firebase_uid": "uid-1",
        "outcome": "compensated",
        "stage": "postgres",
    }


@pytest.mark.asyncio
async def test_inactive_existing_compensation_failure_preserves_pg_failure_and_audit(
    monkeypatch, principal
):
    repo = FakeRepo(existing=_row(active=False), create_error=RuntimeError("original pg failure"))
    provisioner = FakeProvisioner(
        result=SalesProvisioningResult(), revoke_error=RuntimeError("reconcile down")
    )
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    with pytest.raises(HTTPException) as error:
        await admin_routes.create_admin_sales(
            admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Sales"),
            principal,
            repo,
            provisioner,
            FakeAudit(),
        )

    assert error.value.__cause__.args == ("original pg failure",)
    assert provisioner.revoke_calls == 1
    assert audit[0]["action"] == admin_routes.STAFF_AUDIT_ACTION_SALES_RECONCILIATION
    assert audit[0]["detail"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_compensation_failure_is_audited_without_hiding_pg_failure(monkeypatch, principal):
    repo = FakeRepo(create_error=RuntimeError("original pg failure"))
    provisioner = FakeProvisioner(
        result=SalesProvisioningResult(), revoke_error=RuntimeError("reconcile down")
    )
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    with pytest.raises(HTTPException) as error:
        await admin_routes.create_admin_sales(
            admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Sales"),
            principal,
            repo,
            provisioner,
            FakeAudit(),
        )

    assert error.value.__cause__.args == ("original pg failure",)
    assert audit[0]["action"] == admin_routes.STAFF_AUDIT_ACTION_SALES_RECONCILIATION


@pytest.mark.asyncio
async def test_duplicate_active_retry_is_idempotent_and_does_not_revoke(monkeypatch, principal):
    repo = FakeRepo(existing=_row())
    provisioner = FakeProvisioner()
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    response = await admin_routes.create_admin_sales(
        admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Changed"),
        principal,
        repo,
        provisioner,
        FakeAudit(),
    )

    assert response.id == 7
    assert provisioner.provision_calls == 0
    assert provisioner.revoke_calls == 0
    assert repo.create_calls == 0
    assert audit == []


@pytest.mark.asyncio
async def test_successful_creation_records_non_sensitive_audit(monkeypatch, principal):
    repo = FakeRepo()
    provisioner = FakeProvisioner(result=SalesProvisioningResult())
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    response = await admin_routes.create_admin_sales(
        admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Sales"),
        principal,
        repo,
        provisioner,
        FakeAudit(),
    )

    assert response.id == 7
    assert audit[0]["action"] == admin_routes.STAFF_AUDIT_ACTION_SALES_CREATED
    assert audit[0]["detail"] == {"firebase_uid": "uid-1"}


@pytest.mark.asyncio
async def test_firebase_failure_with_reconciliation_failure_returns_503_and_audits(
    monkeypatch, principal
):
    repo = FakeRepo()
    provisioner = FakeProvisioner(
        provision_error=SalesProvisioningError("firebase failed", reconciliation_failed=True)
    )
    audit = []

    async def record(*args, **kwargs):
        audit.append(kwargs)

    monkeypatch.setattr(admin_routes, "record_staff_action", record)

    with pytest.raises(HTTPException) as error:
        await admin_routes.create_admin_sales(
            admin_routes.AdminSalesCreateRequest(firebase_uid="uid-1", full_name="Sales"),
            principal,
            repo,
            provisioner,
            FakeAudit(),
        )

    assert error.value.status_code == 503
    assert audit[0]["action"] == admin_routes.STAFF_AUDIT_ACTION_SALES_RECONCILIATION
