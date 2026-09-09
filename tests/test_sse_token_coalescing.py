"""SSE token coalescing + error-frame retryability tests.

Covers the two /query SSE quality changes:
1. Consecutive sanitized token deltas are merged into fewer ``token`` frames at
   the transport seam (``_sse_stream``), while the concatenated stream stays
   byte-equal to ``done.answer`` and non-token frames keep their ordering.
2. Terminal ``error`` frames carry an additive ``retryable`` boolean.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from api.application.pipelines.workflow import QueryRejected
from api.application.services.token_coalescer import TokenCoalescer
from api.interfaces.api.main import QueryRequest, _sse_stream


class _FakeClock:
    """Manually advanced monotonic clock so the coalescing window is testable."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# ==============================================================================
# TokenCoalescer unit tests — batching window, force flush, empty no-op
# ==============================================================================


def test_coalescer_batches_only_when_size_and_interval_met():
    clock = _FakeClock(0.0)
    c = TokenCoalescer(max_chars=10, min_interval_s=0.06, clock=clock)
    # Below the size cap: buffered, nothing to emit.
    assert c.feed("abc") is None
    # Size cap met but the interval has not elapsed: still buffered.
    assert c.feed("defghij") is None
    clock.advance(0.06)
    # Interval elapsed and size met: the whole buffer is released as one span.
    assert c.feed("k") == "abcdefghijk"
    # Buffer drained: a subsequent flush is a no-op.
    assert c.flush() == ""


def test_coalescer_force_flush_releases_partial_span():
    clock = _FakeClock(0.0)
    c = TokenCoalescer(max_chars=100, min_interval_s=0.06, clock=clock)
    # Well under the size cap, so feed never self-flushes.
    assert c.feed("hello ") is None
    assert c.feed("world") is None
    # A forced flush (non-token event / stream end) releases regardless of window.
    assert c.flush() == "hello world"
    # Empty-buffer flush is a no-op.
    assert c.flush() == ""


def test_coalescer_ignores_empty_feed():
    c = TokenCoalescer(max_chars=1, min_interval_s=0.0, clock=_FakeClock(0.0))
    assert c.feed("") is None
    assert c.flush() == ""


def test_coalescer_preserves_full_concatenation_and_order():
    clock = _FakeClock(0.0)
    c = TokenCoalescer(max_chars=5, min_interval_s=0.0, clock=clock)
    deltas = ["ab", "cd", "ef", "gh", "ij", "kl"]
    emitted: list[str] = []
    for d in deltas:
        span = c.feed(d)
        if span:
            emitted.append(span)
    tail = c.flush()
    if tail:
        emitted.append(tail)
    # Coalescing merges spans but never reorders or drops a character.
    assert "".join(emitted) == "".join(deltas)
    assert len(emitted) < len(deltas)


# ==============================================================================
# _sse_stream transport tests — coalescing + ordering + stream/done equivalence
# ==============================================================================


class _Context:
    anon_token_is_newly_minted = False

    def quota_payload(self):
        return {}


class _Gate:
    async def refund_turn(self, context):
        return None


class _CoalescingPipe:
    """Streams metadata + many tiny deltas, then a done payload whose answer is
    exactly the concatenated deltas (mirrors the sanitized conv output shape)."""

    def __init__(self, pre: list[str], post: list[str]) -> None:
        self._pre = pre
        self._post = post

    async def run(self, *args, on_event=None, **kwargs):
        await on_event("sources", {"sources": [{"doc_id": "d1", "title": "T"}]})
        for d in self._pre:
            await on_event("token", {"text": d})
        await on_event("facts", {"facts": [{"fe_id": "fe-001"}]})
        for d in self._post:
            await on_event("token", {"text": d})
        return {
            "answer": "".join(self._pre) + "".join(self._post),
            "sources": [], "facts": [], "images": [], "videos": [],
            "places": [], "confidence": "HIGH", "requires_review": False,
            "routing": {}, "trace_id": "t", "latency_ms": 1,
        }


class _RejectingPipe:
    async def run(self, *args, **kwargs):
        raise QueryRejected("blocked by policy")


class _CrashingPipe:
    async def run(self, *args, **kwargs):
        raise RuntimeError("boom")


class _StalledPipe:
    async def run(self, *args, **kwargs):
        while True:
            await asyncio.sleep(3600)


def _parse(frame: str) -> tuple[str, dict]:
    if frame.startswith(":"):
        return ("heartbeat", {})
    lines = dict(line.split(": ", 1) for line in frame.strip().split("\n") if ": " in line)
    return (lines.get("event"), json.loads(lines.get("data", "{}")))


async def _collect(stream) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    async for frame in stream:
        out.append(_parse(frame))
    return out


@pytest.mark.asyncio
async def test_sse_stream_coalesces_tokens_and_equals_done_answer():
    pre = [f"p{i:02d}" for i in range(30)]  # 90 chars across 30 deltas
    post = [f"q{i:02d}" for i in range(30)]
    frames = await _collect(
        _sse_stream(
            _CoalescingPipe(pre, post), QueryRequest(query="hello"), None,
            "camellia", None, _Gate(), _Context(),
        )
    )
    names = [e for e, _ in frames]
    # Event names unchanged and terminal done is last.
    assert names[0] == "ack"
    assert names[-1] == "done"
    assert "sources" in names and "facts" in names

    token_frames = [d for e, d in frames if e == "token"]
    total_deltas = len(pre) + len(post)
    # Coalescing collapses many deltas into fewer token frames.
    assert 0 < len(token_frames) < total_deltas

    done = frames[-1][1]
    streamed = "".join(d.get("text", "") for d in token_frames)
    # Concatenated token stream is byte-equal to done.answer.
    assert streamed == done["answer"]

    # Ordering contract: sources precedes facts precedes done, and every token
    # frame emitted before the facts frame carries exactly the pre-facts text
    # (proves the force-flush before a non-token event keeps ordering intact).
    assert names.index("sources") < names.index("facts") < names.index("done")
    pre_facts_text = "".join(
        d.get("text", "") for e, d in frames[: names.index("facts")] if e == "token"
    )
    assert pre_facts_text == "".join(pre)


@pytest.mark.asyncio
async def test_error_frame_rejected_is_not_retryable():
    frames = await _collect(
        _sse_stream(
            _RejectingPipe(), QueryRequest(query="hi"), None,
            "camellia", None, _Gate(), _Context(),
        )
    )
    err = next(d for e, d in frames if e == "error")
    assert err["code"] == "REJECTED"
    assert err["message"] == "blocked by policy"  # existing field preserved
    assert err["retryable"] is False


@pytest.mark.asyncio
async def test_error_frame_internal_is_retryable():
    frames = await _collect(
        _sse_stream(
            _CrashingPipe(), QueryRequest(query="hi"), None,
            "camellia", None, _Gate(), _Context(),
        )
    )
    err = next(d for e, d in frames if e == "error")
    assert err["code"] == "INTERNAL"
    assert err["retryable"] is True


@pytest.mark.asyncio
async def test_error_frame_stream_timeout_is_retryable():
    frames = await _collect(
        _sse_stream(
            _StalledPipe(), QueryRequest(query="hi"), None, "camellia", None,
            _Gate(), _Context(), total_timeout_s=0.3,
        )
    )
    err = next(d for e, d in frames if e == "error")
    assert err["code"] == "STREAM_TIMEOUT"
    assert err["retryable"] is True
    assert frames[-1][0] == "done"
