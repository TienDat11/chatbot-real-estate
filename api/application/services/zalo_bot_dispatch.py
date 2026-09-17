"""Zalo bot event scheduler seam (ZB-01 ingress, ZB-02 processor).

The webhook route must NOT run RAG or provider outbound inline. Instead it
hands a freshly-persisted event_id to a pluggable scheduler via this seam.

House style mirrors api.application.services.anon_identity.set_rate_limit_port:
a module-level port is installed at startup by ZB-03 with the shared brake,
and tests override it freely. With no scheduler registered, schedule() is a documented no-op so
the ingress works in deployments that only accept webhooks without processing.

The durable pending DB row is the correctness boundary. accepted=true means
persisted, never guaranteed processing. Scheduling is best effort: Cloud Run
request-scoped CPU may be unavailable after the ACK, and process shutdown can
lose in-memory tasks. Scheduler failure leaves the row pending for a later
processor; this ingress adds no durable queue or delivery guarantee.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("api.zalo_bot_dispatch")

SchedulerFn = Callable[[int], Awaitable[None]]


async def _default_noop_scheduler(event_id: int) -> None:
    """Default scheduler: ingress-only deployments do not dispatch processing.

    ZB-03 installs the real processor with the shared brake at startup; tests swap
    it for a recording fake. The event is still durably persisted in zalo_bot_events.
    """
    logger.debug("No zalo scheduler registered; event_id=%s is not dispatched", event_id)


# Module-level seam: ZB-03 installs the processor with the shared brake at startup;
# tests swap it for a recording fake.
_scheduler: SchedulerFn = _default_noop_scheduler


def set_scheduler(fn: SchedulerFn) -> None:
    """Install scheduler; ZB-03 registers the processor with the shared brake at startup."""
    global _scheduler
    _scheduler = fn


def get_scheduler() -> SchedulerFn:
    return _scheduler


async def schedule(event_id: int) -> None:
    """Hand a newly-persisted event to the installed scheduler.

    When no scheduler is registered (ingress-only deployment), this is a
    no-op: the event still gets durable storage in zalo_bot_events, but no
    processing dispatch is queued.
    """
    await _scheduler(event_id)
