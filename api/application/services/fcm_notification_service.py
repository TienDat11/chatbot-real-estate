"""FCM delivery orchestration for sales events.

Every dispatch runs concurrent sends under ONE bounded total budget with a
single attempt per token: notification delivery is secondary to the business
event that triggered it, so it must never stretch request latency or pile up
retries behind a slow FCM endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from api.application.ports.fcm_notifications import (
    FcmSender,
    FcmTokenInvalidError,
    FcmTokenRepository,
)

logger = logging.getLogger("api.fcm")

# Total wall-clock budget for one fan-out (all tokens concurrently). Chosen to
# stay under typical LB/proxy timeouts while FCM HTTP v1 p95 is ~1-2s.
FCM_TOTAL_BUDGET_SECONDS = 5.0


@dataclass(frozen=True)
class FcmDeliveryReport:
    """Outcome of one dispatch so callers can inform the UI without logs."""

    tokens_found: int
    dispatched: int
    pruned: int
    failed: int


class FcmNotificationService:
    def __init__(self, tokens: FcmTokenRepository, sender: FcmSender) -> None:
        self.tokens = tokens
        self.sender = sender

    async def notify_sales(
        self, *, sales_id: int, title: str, body: str, data: dict[str, str]
    ) -> FcmDeliveryReport:
        return await self._dispatch(
            await self.tokens.list_fcm_tokens_for_sales(sales_id=sales_id),
            title=title,
            body=body,
            data=data,
        )

    async def resolve_identity_tokens(self, *, identity_type: str, identity_key: str) -> list[str]:
        """Cheap DB-only lookup of enabled devices for one identity.

        Kept separate from dispatch so a request path can answer "does this
        customer have a device?" without paying the FCM round-trip.
        """
        return await self.tokens.list_fcm_tokens(
            identity_type=identity_type, identity_key=identity_key
        )

    async def dispatch_to_tokens(
        self, *, tokens: list[str], title: str | None, body: str | None, data: dict[str, str]
    ) -> FcmDeliveryReport:
        """Fan out to an already-resolved token list (post-response dispatch)."""
        return await self._dispatch(tokens, title=title, body=body, data=data)

    async def notify_identity(
        self,
        *,
        identity_type: str,
        identity_key: str,
        title: str,
        body: str,
        data: dict[str, str],
    ) -> FcmDeliveryReport:
        tokens = await self.resolve_identity_tokens(
            identity_type=identity_type, identity_key=identity_key
        )
        return await self.dispatch_to_tokens(tokens=tokens, title=title, body=body, data=data)

    async def send_test(
        self, *, identity_type: str, identity_key: str, data: dict[str, str]
    ) -> FcmDeliveryReport | None:
        """Ping the caller's OWN devices with a data-only message.

        Used to verify background delivery end-to-end: a data-only payload is
        the only path that deterministically triggers the service worker's
        onBackgroundMessage handler. Returns None when the identity has no
        enabled tokens so the route can answer "no device registered".
        """
        tokens = await self.resolve_identity_tokens(
            identity_type=identity_type, identity_key=identity_key
        )
        if not tokens:
            return None
        return await self._dispatch(
            tokens, title=None, body=None, data=data  # data-only message
        )

    async def _dispatch(
        self, tokens: list[str], *, title: str | None, body: str | None, data: dict[str, str]
    ) -> FcmDeliveryReport:
        if not tokens:
            return FcmDeliveryReport(tokens_found=0, dispatched=0, pruned=0, failed=0)
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        self._send_one(token=token, title=title, body=body, data=data)
                        for token in tokens
                    )
                ),
                timeout=FCM_TOTAL_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            # Budget exhausted: cancel semantics already applied by wait_for;
            # the whole fan-out counts as failed — no retry storm on the next event.
            logger.warning(
                "FCM dispatch budget of %ss exceeded for %s tokens",
                FCM_TOTAL_BUDGET_SECONDS,
                len(tokens),
            )
            return FcmDeliveryReport(
                tokens_found=len(tokens), dispatched=0, pruned=0, failed=len(tokens)
            )
        return FcmDeliveryReport(
            tokens_found=len(tokens),
            dispatched=sum(1 for outcome in outcomes if outcome == "sent"),
            pruned=sum(1 for outcome in outcomes if outcome == "pruned"),
            failed=sum(1 for outcome in outcomes if outcome == "failed"),
        )

    async def _send_one(self, *, token: str, title: str | None, body: str | None, data: dict[str, str]) -> str:
        try:
            await self.sender.send(token=token, title=title, body=body, data=data)
            return "sent"
        except FcmTokenInvalidError:
            # Dead registration: prune so the token stops consuming the cap.
            try:
                await self.tokens.prune_token(token=token)
            except Exception:  # noqa: BLE001 — pruning is best-effort
                logger.warning("FCM token prune failed", exc_info=True)
            return "pruned"
        except Exception:  # noqa: BLE001 — delivery must not fail the business event
            logger.warning("FCM delivery failed", exc_info=True)
            return "failed"


__all__ = ["FCM_TOTAL_BUDGET_SECONDS", "FcmDeliveryReport", "FcmNotificationService"]
