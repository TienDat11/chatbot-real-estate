"""SSE token coalescing — batch consecutive LLM deltas into fewer frames.

The /query SSE stream used to emit one frame per LLM delta, so a single answer
produced hundreds of tiny ``token`` events: high JSON/framing overhead and a
choppy typing effect at the client. This helper lets the transport layer merge
consecutive already-sanitized spans into larger ones without changing the
concatenated text, so the stream/done equivalence invariant holds.

It is deliberately synchronous and allocation-light (O(1) amortized per fed
token): the caller feeds each sanitized delta and receives either ``None`` (the
span is still buffered) or the flushable text to emit. The clock is injectable
so the time window is testable without sleeping.
"""

from __future__ import annotations

import time
from typing import Callable

# Coalescing window defaults. A span is released once it has accumulated at
# least ``DEFAULT_MAX_CHARS`` characters AND at least ``DEFAULT_MIN_INTERVAL_S``
# has elapsed since the previous flush — the size cap bounds frame count while
# the interval cap bounds added latency, so a fast burst batches but a stalled
# stream still surfaces text promptly on the next delta.
DEFAULT_MAX_CHARS = 48
DEFAULT_MIN_INTERVAL_S = 0.060


class TokenCoalescer:
    """Buffer sanitized token deltas and release them in size/time-bounded spans.

    ``feed`` appends one span and returns the buffered text to emit now, or
    ``None`` when it should keep accumulating. ``flush`` force-releases the
    whole buffer (used before any non-token frame and at end of stream) so event
    ordering and the tail-flush contract are preserved. An empty buffer flush is
    a no-op returning ``""``.
    """

    def __init__(
        self,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_chars = max_chars
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._buffer: list[str] = []
        self._buffer_len = 0
        # Anchor the first window at construction so the very first flush is
        # also interval-gated (a burst right at stream start still batches).
        self._last_flush_at = clock()

    def feed(self, text: str) -> str | None:
        """Absorb one sanitized span; return the buffered text if it is due."""
        if not text:
            return None
        self._buffer.append(text)
        self._buffer_len += len(text)
        if (
            self._buffer_len >= self._max_chars
            and (self._clock() - self._last_flush_at) >= self._min_interval_s
        ):
            return self.flush()
        return None

    def flush(self) -> str:
        """Force-release everything buffered; reset the interval window."""
        if not self._buffer:
            return ""
        span = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_len = 0
        self._last_flush_at = self._clock()
        return span


__all__ = ["TokenCoalescer", "DEFAULT_MAX_CHARS", "DEFAULT_MIN_INTERVAL_S"]
