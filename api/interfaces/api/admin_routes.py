"""Staff session endpoints on Firebase ID-token auth (story 8.3 / ISSUE-06).

GET /api/admin/session — principal echo behind ``require_admin`` (admin screen
bootstrap) and GET /api/sales/session — principal echo behind ``require_sales``
(broker-board bootstrap). The FE calls these right after sign-in to learn its
effective identity (verified uid, role, and the PG sales mapping) before
rendering role-gated screens; both endpoints are pure reads of the
dependency-resolved principal, no business logic.

POST /api/admin/projects/{project_key}/reengage-run — manual trigger of
ReengageMatchWorkflow (story 9.4 / ISSUE-10): matches previously-rejected,
marketing-consented customers against the activated project and enqueues
re-approach suggestions. ISSUE-13's publish flow will call the same workflow;
until that lands this route is the operator entry point.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.application.pipelines.reengage_workflow import (
    ProjectActivation,
    run_reengage_matching_for_activated_project,
)
from api.application.ports.embedding import NeedProfileEmbeddingNotConfiguredError
from api.application.ports.reengage_queue import ReengageQueueNotConfiguredError
from api.application.ports.sales_provisioning import SalesProvisioner, SalesProvisioningError
from api.application.ports.staff_audit import (
    STAFF_AUDIT_ACTION_REENGAGE_RUN_TRIGGERED,
    STAFF_AUDIT_ACTION_SALES_CREATED,
    STAFF_AUDIT_ACTION_SALES_RECONCILIATION,
    STAFF_AUDIT_ACTION_SALES_STATUS_UPDATED,
    StaffAuditStore,
)
from api.application.services.staff_audit_service import record_staff_action
from api.infrastructure.adapters.postgres_leads import get_lead_pool
from api.infrastructure.dependencies import (
    get_need_profile_embedding,
    get_reengage_queue_store,
    get_sales_provisioner,
    get_staff_audit_store,
)
from api.infrastructure.ports.leads import LeadRepository, SalesRow, get_lead_repository
from api.interfaces.api.deps import AuthenticatedPrincipal, require_admin, require_sales


class AuthenticatedSessionResponse(BaseModel):
    """Wire shape of a session bootstrap: the principal fields verbatim."""

    firebase_uid: str
    email: str | None
    role: str
    sales_id: int | None


class ReengageRunRequest(BaseModel):
    """Optional enrichment of the activated project beyond its registry row."""

    display_name: str | None = None
    description: str = ""
    price_min_vnd: int | None = Field(default=None, ge=0)
    price_max_vnd: int | None = Field(default=None, ge=0)


class ReengageQueueEntryResponse(BaseModel):
    """One queued re-approach suggestion as the CRM will consume it."""

    customer_id: str
    project_key: str
    similarity_score: float
    rejection_reason: str | None
    budget_vnd: int | None
    attempt_count: int


class ReengageRunResponse(BaseModel):
    queued_count: int
    entries: list[ReengageQueueEntryResponse]
    activated_project_key: str
    matched_at: str


admin_session_router = APIRouter(prefix="/api/admin", tags=["admin-session"])
sales_session_router = APIRouter(prefix="/api/sales", tags=["sales-session"])


class AdminSalesCreateRequest(BaseModel):
    firebase_uid: str = Field(..., min_length=1, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    priority: int = Field(default=0, ge=0, le=100)


class AdminSalesPatchRequest(BaseModel):
    is_active: bool


class AdminSalesBindRequest(BaseModel):
    firebase_uuid: str = Field(..., min_length=1, max_length=128)


class AdminSalesResponse(BaseModel):
    id: int
    firebase_uid: str | None
    full_name: str
    phone: str | None
    is_active: bool
    priority: int


def _sales_response(row: SalesRow) -> AdminSalesResponse:
    return AdminSalesResponse(
        id=row.id,
        firebase_uid=row.firebase_uid,
        full_name=row.full_name,
        phone=row.phone,
        is_active=row.is_active,
        priority=row.priority,
    )


@admin_session_router.get("/sales", response_model=list[AdminSalesResponse])
async def list_admin_sales(
    _: AuthenticatedPrincipal = Depends(require_admin),
    repo: LeadRepository = Depends(get_lead_repository),
) -> list[AdminSalesResponse]:
    return [_sales_response(row) for row in await repo.list_sales()]


@admin_session_router.post("/sales", response_model=AdminSalesResponse, status_code=201)
async def create_admin_sales(
    payload: AdminSalesCreateRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin),
    repo: LeadRepository = Depends(get_lead_repository),
    provisioner: SalesProvisioner = Depends(get_sales_provisioner),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
) -> AdminSalesResponse:
    existing = await repo.get_sales_for_admin(payload.firebase_uid)
    if existing is not None and existing.is_active:
        return _sales_response(existing)

    provisioned = False
    try:
        provisioning_result = await provisioner.provision(
            firebase_uid=payload.firebase_uid, full_name=payload.full_name
        )
        # Compensate whenever this request elevated Firebase access, including
        # retries that repair an inactive existing PG mapping. Preserve access
        # that was already legitimately active before this request.
        provisioned = not bool(getattr(provisioning_result, "pre_existing_active", False))
        row = await repo.create_sales_mapping(
            firebase_uid=payload.firebase_uid,
            full_name=payload.full_name,
            phone=payload.phone,
            priority=payload.priority,
        )
    except SalesProvisioningError as exc:
        if exc.reconciliation_failed:
            await record_staff_action(
                audit_store,
                principal=principal,
                action=STAFF_AUDIT_ACTION_SALES_RECONCILIATION,
                detail={
                    "firebase_uid": payload.firebase_uid,
                    "outcome": "failed",
                    "stage": "firebase",
                },
            )
        raise HTTPException(status_code=503, detail="Sales provisioning unavailable") from exc
    except Exception as original:
        if provisioned:
            try:
                await provisioner.revoke(firebase_uid=payload.firebase_uid)
            except Exception:
                await record_staff_action(
                    audit_store,
                    principal=principal,
                    action=STAFF_AUDIT_ACTION_SALES_RECONCILIATION,
                    detail={
                        "firebase_uid": payload.firebase_uid,
                        "outcome": "failed",
                        "stage": "postgres",
                    },
                )
                raise HTTPException(
                    status_code=503, detail="Sales mapping unavailable"
                ) from original
            await record_staff_action(
                audit_store,
                principal=principal,
                action=STAFF_AUDIT_ACTION_SALES_RECONCILIATION,
                detail={
                    "firebase_uid": payload.firebase_uid,
                    "outcome": "compensated",
                    "stage": "postgres",
                },
            )
        raise HTTPException(status_code=503, detail="Sales mapping unavailable") from original
    await record_staff_action(
        audit_store,
        principal=principal,
        action=STAFF_AUDIT_ACTION_SALES_CREATED,
        detail={"firebase_uid": payload.firebase_uid},
    )
    return _sales_response(row)


@admin_session_router.post("/sales/{sales_id}/bind-firebase-uid", response_model=AdminSalesResponse)
async def bind_admin_sales_firebase_uid(
    sales_id: int,
    payload: AdminSalesBindRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
) -> AdminSalesResponse:
    """Bind a Firebase identity to an existing sales row by its numeric id."""
    pool = await get_lead_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE sales SET firebase_uid = $2, updated_at = now()
                   WHERE id = $1 AND firebase_uid IS NULL
                   RETURNING id, access_key, full_name, role, phone, is_active,
                             priority, last_seen_at, firebase_uid,
                             NULL::timestamptz AS last_assigned_at""",
                sales_id,
                payload.firebase_uuid,
            )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise HTTPException(status_code=409, detail="Firebase UID already mapped") from exc
    if row is None:
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM sales WHERE id = $1", sales_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Sales account not found")
        raise HTTPException(status_code=409, detail="Sales account already has a Firebase UID")

    sales_row = SalesRow(**dict(row))
    await record_staff_action(
        audit_store,
        principal=principal,
        action=STAFF_AUDIT_ACTION_SALES_RECONCILIATION,
        detail={"sales_id": sales_id, "firebase_uid": payload.firebase_uuid, "outcome": "bound"},
    )
    return _sales_response(sales_row)


@admin_session_router.patch("/sales/{firebase_uid}", response_model=AdminSalesResponse)
async def patch_admin_sales(
    firebase_uid: str,
    payload: AdminSalesPatchRequest,
    principal: AuthenticatedPrincipal = Depends(require_admin),
    repo: LeadRepository = Depends(get_lead_repository),
    provisioner: SalesProvisioner = Depends(get_sales_provisioner),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
) -> AdminSalesResponse:
    if not payload.is_active:
        # Revoke the external access gate first. PG must never remain active
        # when Firebase or Firestore still permits sales access.
        existing = await repo.get_sales_for_admin(firebase_uid)
        if existing is None:
            raise HTTPException(status_code=404, detail="Sales account not found")
        try:
            await provisioner.revoke(firebase_uid=firebase_uid)
            row = await repo.set_sales_active(firebase_uid, is_active=False)
            if row is None:
                raise RuntimeError("sales mapping disappeared")
        except SalesProvisioningError as exc:
            await record_staff_action(
                audit_store,
                principal=principal,
                action=STAFF_AUDIT_ACTION_SALES_STATUS_UPDATED,
                detail={
                    "firebase_uid": firebase_uid,
                    "is_active": False,
                    "outcome": "failed",
                    "stage": "firebase",
                },
            )
            raise HTTPException(status_code=503, detail="Sales revocation unavailable") from exc
        except Exception as exc:
            # If PG could not be made inactive, restore the external gates so
            # the account is not left in a split state.
            try:
                await provisioner.provision(firebase_uid=firebase_uid, full_name=existing.full_name)
            except SalesProvisioningError:
                pass
            await record_staff_action(
                audit_store,
                principal=principal,
                action=STAFF_AUDIT_ACTION_SALES_STATUS_UPDATED,
                detail={
                    "firebase_uid": firebase_uid,
                    "is_active": False,
                    "outcome": "failed",
                    "stage": "postgres",
                },
            )
            raise HTTPException(status_code=503, detail="Sales status update unavailable") from exc
    else:
        existing = await repo.get_sales_for_admin(firebase_uid)
        if existing is None:
            raise HTTPException(status_code=404, detail="Sales account not found")
        try:
            await provisioner.provision(firebase_uid=firebase_uid, full_name=existing.full_name)
            row = await repo.set_sales_active(firebase_uid, is_active=True)
            if row is None:
                raise RuntimeError("sales mapping disappeared")
        except SalesProvisioningError as exc:
            try:
                await repo.set_sales_active(firebase_uid, is_active=False)
            except Exception:
                pass
            await record_staff_action(
                audit_store,
                principal=principal,
                action=STAFF_AUDIT_ACTION_SALES_STATUS_UPDATED,
                detail={
                    "firebase_uid": firebase_uid,
                    "is_active": True,
                    "outcome": "failed",
                    "stage": "firebase",
                },
            )
            raise HTTPException(status_code=503, detail="Sales provisioning unavailable") from exc
        except Exception as exc:
            try:
                await provisioner.revoke(firebase_uid=firebase_uid)
            except SalesProvisioningError:
                pass
            try:
                await repo.set_sales_active(firebase_uid, is_active=False)
            except Exception:
                pass
            await record_staff_action(
                audit_store,
                principal=principal,
                action=STAFF_AUDIT_ACTION_SALES_STATUS_UPDATED,
                detail={
                    "firebase_uid": firebase_uid,
                    "is_active": True,
                    "outcome": "failed",
                    "stage": "postgres",
                },
            )
            raise HTTPException(status_code=503, detail="Sales status update unavailable") from exc

    await record_staff_action(
        audit_store,
        principal=principal,
        action=STAFF_AUDIT_ACTION_SALES_STATUS_UPDATED,
        detail={"firebase_uid": firebase_uid, "is_active": payload.is_active},
    )
    return _sales_response(row)


@admin_session_router.post("/projects/{project_key}/publish", status_code=501)
async def publish_project(_: AuthenticatedPrincipal = Depends(require_admin)) -> None:
    raise HTTPException(status_code=501, detail="Project publishing orchestration is not available")


@admin_session_router.post("/projects/{project_key}/unpublish", status_code=501)
async def unpublish_project(_: AuthenticatedPrincipal = Depends(require_admin)) -> None:
    raise HTTPException(
        status_code=501, detail="Project unpublishing orchestration is not available"
    )


@admin_session_router.get("/session", response_model=AuthenticatedSessionResponse)
async def read_admin_session(
    authenticated_principal: AuthenticatedPrincipal = Depends(require_admin),
) -> AuthenticatedSessionResponse:
    """Echo the admin principal so the FE can bootstrap the admin screen."""
    return AuthenticatedSessionResponse(
        firebase_uid=authenticated_principal.firebase_uid,
        email=authenticated_principal.email,
        role=authenticated_principal.role,
        sales_id=authenticated_principal.sales_id,
    )


@sales_session_router.get("/session", response_model=AuthenticatedSessionResponse)
async def read_sales_session(
    authenticated_principal: AuthenticatedPrincipal = Depends(require_sales),
) -> AuthenticatedSessionResponse:
    """Echo the sales principal so the FE can bootstrap the broker board."""
    return AuthenticatedSessionResponse(
        firebase_uid=authenticated_principal.firebase_uid,
        email=authenticated_principal.email,
        role=authenticated_principal.role,
        sales_id=authenticated_principal.sales_id,
    )


@admin_session_router.post(
    "/projects/{project_key}/reengage-run",
    response_model=ReengageRunResponse,
)
async def trigger_reengage_run_for_project(
    project_key: str,
    request_body: ReengageRunRequest,
    _authenticated_principal: AuthenticatedPrincipal = Depends(require_admin),
    audit_store: StaffAuditStore = Depends(get_staff_audit_store),
) -> ReengageRunResponse:
    """Manually fire ReengageMatchWorkflow for one activated project.

    Unconfigured optional infrastructure (no embedding key, no firebase
    binding) is an operator-facing condition, not a crash: 503 with the
    reason instead of a mid-workflow failure.
    """
    project_activation = ProjectActivation(
        project_key=project_key,
        display_name=request_body.display_name or project_key,
        description=request_body.description,
        price_min_vnd=request_body.price_min_vnd,
        price_max_vnd=request_body.price_max_vnd,
    )
    try:
        workflow_result = await run_reengage_matching_for_activated_project(
            project_activation,
            lead_repository=await get_lead_repository(),
            need_profile_embedding=await get_need_profile_embedding(),
            reengage_queue_store=await get_reengage_queue_store(),
        )
    except (NeedProfileEmbeddingNotConfiguredError, ReengageQueueNotConfiguredError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    # Only successful runs are audited: a failed trigger changed nothing.
    await record_staff_action(
        audit_store,
        principal=_authenticated_principal,
        action=STAFF_AUDIT_ACTION_REENGAGE_RUN_TRIGGERED,
        detail={
            "project_key": project_key,
            "queued_count": workflow_result["queued_count"],
        },
    )
    return ReengageRunResponse(
        queued_count=workflow_result["queued_count"],
        entries=[
            ReengageQueueEntryResponse(
                customer_id=entry.customer_id,
                project_key=entry.project_key,
                similarity_score=entry.similarity_score,
                rejection_reason=entry.rejection_reason,
                budget_vnd=entry.budget_vnd,
                attempt_count=entry.attempt_count,
            )
            for entry in workflow_result["entries"]
        ],
        activated_project_key=workflow_result["activated_project_key"],
        matched_at=workflow_result["matched_at"],
    )
