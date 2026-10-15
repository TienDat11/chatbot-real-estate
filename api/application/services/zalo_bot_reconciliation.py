"""Serial durable-inbox sweeps; wakeups are hints, never queue ownership."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("api.zalo_bot_reconciliation")


@dataclass
class ZaloRuntime:
    store: Any
    processor: Any
    settings: Any
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    stopping: bool = False

    async def wake(self, event_id: int) -> None:
        """Ignore event ID: database selection also recovers missed wakeups."""
        if not self.stopping:
            self.signal.set()

    async def run(self) -> None:
        """Sweep immediately, then wait for a hint or the bounded poll interval."""
        while not self.stopping and not self.processor.halted and self.settings.zalo_bot_enabled:
            # A wake during the sweep remains set for the following wait.
            self.signal.clear()

            try:
                if bool(getattr(self.settings, "zalo_bot_maintenance_enabled", True)):
                    exhausted, cleaned = await self.store.maintain()
                    if exhausted or cleaned:
                        logger.info("ZALO_MAINTENANCE exhausted=%s cleaned=%s", exhausted, cleaned)
                if self.stopping or self.processor.halted or not self.settings.zalo_bot_enabled:
                    break
                candidates = await self.store.due_candidates()
                for event_id in candidates[: self.settings.zalo_bot_batch_size]:
                    if self.stopping or self.processor.halted or not self.settings.zalo_bot_enabled:
                        break
                    await self.processor.process(event_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never expose database errors, raw routing or exception text.
                logger.warning("ZALO_SWEEP_UNAVAILABLE")
            if self.stopping or self.processor.halted or not self.settings.zalo_bot_enabled:
                break
            try:
                await asyncio.wait_for(self.signal.wait(), timeout=self.settings.zalo_bot_poll_seconds)
            except TimeoutError:
                pass
