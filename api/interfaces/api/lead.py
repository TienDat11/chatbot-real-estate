"""Customer-facing lead submission API (Epic 5/6 Story 6.4 + story 10.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.application.services.lead_service import (
    DuplicateLeadError,
    create_customer_lead,
    normalize_phone,
    validate_phone,
)
from api.application.services.project_scope import (
    ProjectScopeError,
    validate_project_key,
)
from api.infrastructure.ports.leads import LeadRepository, get_lead_repository

router = APIRouter(prefix="/api", tags=["lead"])


class LeadSubmitRequest(BaseModel):
    # G1: project_key is REQUIRED — the lead's project is read from the request,
    # never guessed from the session (session_id is optional and may not resolve).
    project_key: str
    session_id: str | None = Field(default=None, max_length=128)
    # D7: anonymous persistent device id (UUID v4) — PII once paired with phone.
    device_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=50)
    phone: str = Field(..., min_length=9, max_length=20)
    consent: bool
    note: str | None = Field(default=None, max_length=200)
    budget_vnd: int | None = Field(default=None, ge=0)

    @field_validator("project_key")
    @classmethod
    def project_key_is_valid(cls, value: str) -> str:
        try:
            validate_project_key(value)
        except ProjectScopeError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("phone")
    @classmethod
    def phone_is_vietnamese(cls, value: str) -> str:
        value = normalize_phone(value)
        if not validate_phone(value):
            raise ValueError("Số điện thoại chưa đúng định dạng Việt Nam")
        return value

    @field_validator("name", "note")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class LeadSubmitResponse(BaseModel):
    lead_id: int
    will_call_within_minutes: int = 5


@router.post("/lead", response_model=LeadSubmitResponse, status_code=201)
async def submit_lead(
    payload: LeadSubmitRequest,
    repo: LeadRepository = Depends(get_lead_repository),
) -> LeadSubmitResponse:
    """Validate consent and phone, persist the lead, and assign a sales owner."""
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Consent is required")
    try:
        lead = await create_customer_lead(
            repo,
            session_id=payload.session_id,
            project_key=payload.project_key,
            device_id=payload.device_id,
            name=payload.name,
            phone=payload.phone,
            consent=payload.consent,
            note=payload.note,
            budget_vnd=payload.budget_vnd,
        )
    except DuplicateLeadError as exc:
        # QA D3: mirror the FE contract (submitLead maps 409 -> "duplicate");
        # detail is a structured object so clients can tell dedup from other
        # conflicts without parsing human text.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_lead",
                "message": "This phone number already registered for this project recently.",
                "lead_id": exc.lead_id,
                "created_at": exc.created_at.isoformat(),
            },
        ) from exc
    return LeadSubmitResponse(lead_id=lead.id)
