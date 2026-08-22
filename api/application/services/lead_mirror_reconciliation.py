"""Reconciliation sweep for the PG-to-Firestore lead mirror (story 9.2).

The dual-write is best-effort, so a Firestore blip leaves the lead row in
``pending``/``failed``. This sweep re-runs the same mirror service for stale
rows until the mirror converges with PG. It is registered as a background
asyncio task ONLY when the firebase binding is 'firestore' AND the
reconciliation flag is on — the off-binding default pays zero cost.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from api.application.services.lead_mirror_service import (
    MIRROR_STATUS_DONE,
    sync_lead_mirror_after_commit,
)
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.leads import LeadRepository, get_lead_repository
from api.infrastructure.ports.realtime_mirror import (
    RealtimeLeadMirror,
    get_realtime_lead_mirror,
)

logger = logging.getLogger("api.lead_mirror_reconciliation")

# Floor the loop interval so a misconfigured tiny interval cannot busy-loop
# the event loop against Firestore rate limits.
_MIN_SWEEP_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class MirrorSweepOutcome:
    """One sweep's counts, for logging and test assertions."""

    retried: int
    converged: int
    still_pending_or_failed: int


async def sweep_stale_lead_mirrors_once(
    *,
    repo: LeadRepository,
    mirror: RealtimeLeadMirror,
    stale_before: datetime,
) -> MirrorSweepOutcome:
    """Retry one bounded batch of stale mirror writes; returns the outcome.

    Rows that fail again simply stay ``pending``/``failed`` and are picked up
    by the next sweep — convergence is eventual, not per-sweep.
    """
    stale_leads = await repo.list_stale_mirror_leads(
        stale_before=stale_before,
        limit=get_settings().lead_mirror_stale_batch_limit,
    )
    converged_count = 0
    for stale_lead in stale_leads:
        final_status = await sync_lead_mirror_after_commit(
            stale_lead, repo=repo, mirror=mirror
        )
        if final_status == MIRROR_STATUS_DONE:
            converged_count += 1
    return MirrorSweepOutcome(
        retried=len(stale_leads),
        converged=converged_count,
        still_pending_or_failed=len(stale_leads) - converged_count,
    )


async def run_lead_mirror_reconciliation_loop() -> None:
    """Periodic sweep entry point; runs forever until the task is cancelled.

    Resolves the repository and mirror through the port factories each cycle
    so a config change (or a rebuilt singleton) is honored without restart.
    Any per-sweep failure is logged and swallowed — the loop must survive.
    """
    settings = get_settings()
    sweep_interval_seconds = max(
        _MIN_SWEEP_INTERVAL_SECONDS, settings.lead_mirror_sweep_interval_seconds
    )
    while True:
        await asyncio.sleep(sweep_interval_seconds)
        try:
            repo = await get_lead_repository()
            mirror = await get_realtime_lead_mirror()
            stale_before = datetime.now(timezone.utc) - timedelta(
                minutes=settings.lead_mirror_stale_after_minutes
            )
            outcome = await sweep_stale_lead_mirrors_once(
                repo=repo, mirror=mirror, stale_before=stale_before
            )
            if outcome.retried:
                logger.info(
                    "lead mirror sweep: retried=%s converged=%s remaining=%s",
                    outcome.retried,
                    outcome.converged,
                    outcome.still_pending_or_failed,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive any sweep failure
            logger.warning("lead mirror sweep crashed", exc_info=True)
