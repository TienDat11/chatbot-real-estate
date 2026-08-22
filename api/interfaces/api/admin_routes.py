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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.application.pipelines.reengage_workflow import (
    ProjectActivation,
    run_reengage_matching_for_activated_project,
)
from api.application.ports.embedding import NeedProfileEmbeddingNotConfiguredError
from api.application.ports.reengage_queue import ReengageQueueNotConfiguredError
from api.application.ports.staff_audit import (
    STAFF_AUDIT_ACTION_REENGAGE_RUN_TRIGGERED,
    StaffAuditStore,
)
from api.application.services.staff_audit_service import record_staff_action
from api.infrastructure.dependencies import (
    get_need_profile_embedding,
    get_reengage_queue_store,
    get_staff_audit_store,
)
from api.infrastructure.ports.leads import get_lead_repository
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
