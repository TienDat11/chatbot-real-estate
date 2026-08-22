"""No-op realtime mirror — used when the firebase binding is off (default)."""

from __future__ import annotations

from api.infrastructure.ports.realtime_mirror import LeadMirrorDocument, RealtimeLeadMirror


class NoopRealtimeLeadMirror(RealtimeLeadMirror):
    """Swallows mirror writes so callers need no binding awareness; health stays True."""

    async def upsert_lead_mirror(self, *, customer_id: str, document: LeadMirrorDocument) -> None:
        return None

    async def remove_lead_mirror(self, customer_id: str) -> None:
        return None

    async def health_check(self) -> bool:
        return True
