"""Customer-facing lead submission API (Epic 5/6 Story 6.4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.application.services.lead_service import (
    create_customer_lead,
    normalize_phone,
    validate_phone,
)
from api.infrastructure.ports.leads import LeadRepository, get_lead_repository

router = APIRouter(prefix="/api", tags=["lead"])


class LeadSubmitRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=50)
    phone: str = Field(..., min_length=9, max_length=20)
    consent: bool
    note: str | None = Field(default=None, max_length=200)
    budget_vnd: int | None = Field(default=None, ge=0)

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
    lead = await create_customer_lead(
        repo,
        session_id=payload.session_id,
        name=payload.name,
        phone=payload.phone,
        consent=payload.consent,
        note=payload.note,
        budget_vnd=payload.budget_vnd,
    )
    return LeadSubmitResponse(lead_id=lead.id)
