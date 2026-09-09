"""In-process sliding-window brake for successful phone reveals (G3-r6).

Contract (spec §10.2): at most 10 SUCCESSFUL reveals per minute per actor;
an exceeded budget answers 429 with ``Retry-After``. Only successful HTTP
200 outcomes consume budget — a 403/404 probe costs nothing, matching the
"10 successful requests/minute/actor" wording and keeping authorization
failures from locking an actor out of their own legitimate reveal.

The window is process-local by design: the staff reveal surface is a
low-volume CRM action, and a durable cross-worker brake would need a new
table (out of scope for G3-r6 — ISSUE-G4-01 owns DDL). Worst case under
N workers is 10*N successful reveals/minute/actor, still bounded; the
audit trail remains the authoritative per-reveal record.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

REVEAL_SUCCESS_BUDGET_PER_MINUTE = 10
REVEAL_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int


class SlidingWindowRateLimiter:
    """Fixed-window-free sliding limiter keyed by an opaque actor id."""

    def __init__(
        self,
        *,
        limit: int = REVEAL_SUCCESS_BUDGET_PER_MINUTE,
        window_seconds: float = REVEAL_WINDOW_SECONDS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, actor_key: str) -> RateLimitDecision:
        """Read-only budget probe; never consumes budget."""
        now = time.monotonic()
        with self._lock:
            stamps = [
                stamp for stamp in self._events.get(actor_key, []) if now - stamp < self._window
            ]
            self._events[actor_key] = stamps
            if len(stamps) < self._limit:
                return RateLimitDecision(allowed=True, retry_after_seconds=0)
            retry_after = self._window - (now - stamps[0])
            return RateLimitDecision(
                allowed=False, retry_after_seconds=max(1, int(retry_after) + 1)
            )

    def record_success(self, actor_key: str) -> None:
        now = time.monotonic()
        with self._lock:
            stamps = [
                stamp for stamp in self._events.get(actor_key, []) if now - stamp < self._window
            ]
            stamps.append(now)
            self._events[actor_key] = stamps

    def reset(self) -> None:
        """Test seam: drop every recorded window."""
        with self._lock:
            self._events.clear()


# Single shared limiter for the reveal route (module-level, like the other
# best-effort in-process state in this service layer).
reveal_rate_limiter = SlidingWindowRateLimiter()


class RevealRateLimitedError(Exception):
    """Budget exhausted; carries the Retry-After seconds for the route."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Phone reveal rate limit exceeded")


def enforce_reveal_rate_limit(actor_uid: str) -> None:
    """Raise when this actor's successful-reveal budget is exhausted."""
    decision = reveal_rate_limiter.check(actor_uid)
    if not decision.allowed:
        raise RevealRateLimitedError(decision.retry_after_seconds)


def record_successful_reveal(actor_uid: str) -> None:
    """Consume one budget unit for a reveal that answered 200."""
    reveal_rate_limiter.record_success(actor_uid)


__all__ = [
    "REVEAL_SUCCESS_BUDGET_PER_MINUTE",
    "REVEAL_WINDOW_SECONDS",
    "RateLimitDecision",
    "RevealRateLimitedError",
    "SlidingWindowRateLimiter",
    "enforce_reveal_rate_limit",
    "record_successful_reveal",
    "reveal_rate_limiter",
]
