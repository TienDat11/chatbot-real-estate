"""Sales API — broker board endpoints (Epic 5/6 Story 6.4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api.application.services.lead_service import (
    get_sales_dashboard,
    handle_lead_action,
)
from api.infrastructure.ports.leads import LeadRepository, get_lead_repository, SalesRow


router = APIRouter(prefix="/api/sales", tags=["sales"])


# ----- Auth dependency -----

async def verify_sales_key(
    x_sales_key: str = Header(..., alias="X-Sales-Key"),
    repo: LeadRepository = Depends(get_lead_repository),
) -> SalesRow:
    """Validate X-Sales-Key header, return sales row, update last_seen."""
    sales = await repo.get_sales_by_key(x_sales_key)
    if not sales:
        raise HTTPException(status_code=401, detail="Invalid or inactive sales key")
    await repo.update_sales_last_seen(sales.id)
    return sales


# ----- Models -----

class LeadActionRequest(BaseModel):
    action: str = Field(..., pattern="^(called|no_answer|callback|booked|lost)$")
    note: str | None = None


class LeadActionResponse(BaseModel):
    ok: bool
    lead_status: str | None = None


class LeadStatusPatchRequest(BaseModel):
    status: str = Field(..., pattern="^(called|callback|booked|lost)$")
    note: str | None = None


class LeadStatusPatchResponse(BaseModel):
    ok: bool
    lead_id: int
    lead_status: str


# ----- Endpoints -----

@router.get("/leads")
async def get_leads(
    sales: SalesRow = Depends(verify_sales_key),
    repo: LeadRepository = Depends(get_lead_repository),
) -> dict[str, Any]:
    """Active leads assigned to this sales, sorted by lock_expires_at ASC."""
    return await get_sales_dashboard(repo, sales)


@router.post("/leads/{lead_id}/action", response_model=LeadActionResponse)
async def lead_action(
    lead_id: int,
    payload: LeadActionRequest,
    sales: SalesRow = Depends(verify_sales_key),
    repo: LeadRepository = Depends(get_lead_repository),
) -> LeadActionResponse:
    """Process sales action on their lead."""
    lead = await handle_lead_action(repo, sales, lead_id, payload.action, payload.note)
    if not lead:
        raise HTTPException(
            status_code=409,
            detail="Lead not found, not assigned to you, or not in actionable state",
        )
    return LeadActionResponse(ok=True, lead_status=lead.status)


@router.patch("/leads/{lead_id}/status", response_model=LeadStatusPatchResponse)
async def patch_lead_status(
    lead_id: int,
    payload: LeadStatusPatchRequest,
    sales: SalesRow = Depends(verify_sales_key),
    repo: LeadRepository = Depends(get_lead_repository),
) -> LeadStatusPatchResponse:
    """Broker marks lead as called/callback/booked/lost via PATCH (literal prompt contract)."""
    lead = await repo.get_lead_by_id(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.status == "new" and payload.status == "called":
        result = await handle_lead_action(repo, sales, lead_id, "called", payload.note)
        if not result:
            raise HTTPException(status_code=409, detail="Lead not assigned to you")
        return LeadStatusPatchResponse(ok=True, lead_id=lead_id, lead_status="called")
    if payload.status in ("callback", "booked", "lost"):
        action = "callback" if payload.status == "callback" else payload.status
        result = await handle_lead_action(repo, sales, lead_id, action, payload.note)
        if not result:
            raise HTTPException(status_code=409, detail="Lead not in actionable state")
        return LeadStatusPatchResponse(ok=True, lead_id=lead_id, lead_status=result.status)
    raise HTTPException(status_code=422, detail="Invalid status transition from current state")


@router.get("/stats")
async def get_stats(
    sales: SalesRow = Depends(verify_sales_key),
    repo: LeadRepository = Depends(get_lead_repository),
) -> dict[str, Any]:
    """Today's stats for this sales."""
    return await get_sales_dashboard(repo, sales)
