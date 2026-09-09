"""Focused SSE keep-alive and cleanup contract tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from api.interfaces.api.main import QueryRequest, _sse_stream


class _Context:
    anon_token_is_newly_minted = False

    def quota_payload(self):
        return {}


class _Gate:
    async def refund_turn(self, context):
        return None


class _Pipe:
    async def run(self, *args, **kwargs):
        await asyncio.sleep(16)
        return {
            "answer": "ok", "sources": [], "facts": [], "images": [], "videos": [],
            "places": [], "confidence": "HIGH", "requires_review": False,
            "routing": {}, "trace_id": "t", "latency_ms": 1,
        }


class _StalledPipe:
    """Pipeline that never settles — simulates a stalled LLM provider."""

    async def run(self, *args, **kwargs):
        while True:
            await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_sse_emits_parser_safe_heartbeat_before_completion():
    frames = []
    async for frame in _sse_stream(
        _Pipe(), QueryRequest(query="hello"), None, "camellia", None, _Gate(), _Context()
    ):
        frames.append(frame)
        if frame.startswith("event: done"):
            break
    assert any(frame == ": heartbeat\n\n" for frame in frames)
    assert frames[-1].startswith("event: done")


@pytest.mark.asyncio
async def test_sse_stalled_pipeline_terminates_by_total_deadline():
    """A stalled provider must terminate within the total deadline.

    The stream must ship a terminal error (STREAM_TIMEOUT) + done frame instead
    of emitting heartbeats forever; the deadline is a wall clock, so the
    heartbeat path can never extend the stream past it.
    """
    started = asyncio.get_event_loop().time()
    frames = []
    async for frame in _sse_stream(
        _StalledPipe(), QueryRequest(query="hello"), None, "camellia", None,
        _Gate(), _Context(), total_timeout_s=0.5,
    ):
        frames.append(frame)
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 5.0
    assert frames[0].startswith("event: ack")
    assert any(
        frame.startswith("event: error")
        and "STREAM_TIMEOUT" in frame
        and "thử lại" in frame
        for frame in frames
    )
    assert frames[-1].startswith("event: done")


@pytest.mark.asyncio
async def test_sse_heartbeats_do_not_reset_total_deadline():
    """Heartbeats must NOT extend the total deadline.

    A pipe that keeps the stream alive only through the 15s heartbeat silence
    (never completing) is still cut at the deadline with a terminal frame.
    total_timeout_s is small enough that no heartbeat can fire (15s wait) — the
    deadline, not the heartbeat, must be what ends the stream.
    """
    frames = []
    async for frame in _sse_stream(
        _StalledPipe(), QueryRequest(query="hello"), None, "camellia", None,
        _Gate(), _Context(), total_timeout_s=0.25,
    ):
        frames.append(frame)
    assert not any(frame == ": heartbeat\n\n" for frame in frames)
    assert any("STREAM_TIMEOUT" in frame for frame in frames)
    assert frames[-1].startswith("event: done")


class _RecordingGate:
    """Quota-gate double that counts refund/finalize calls (settlement guard)."""

    def __init__(self) -> None:
        self.refund_calls = 0
        self.finalize_calls = 0

    async def refund_turn(self, context):
        self.refund_calls += 1
        return None

    async def finalize_turn(self, context):
        self.finalize_calls += 1
        return True


class _ReservedContext:
    """Anonymous/customer-like context: carries a reservation that must be
    refunded on failure and finalized on success (spec §4 R1)."""

    identity_key = "anon:test"
    project_key = "camellia"
    anon_token_is_newly_minted = False

    def quota_payload(self):
        return {}


class _CrashingPipe:
    """Pipeline that raises before any await — deterministic refund path."""

    async def run(self, *args, **kwargs):
        raise RuntimeError("boom")


class _QuickPipe:
    """Pipeline that completes immediately with a valid payload."""

    async def run(self, *args, **kwargs):
        return {
            "answer": "ok", "sources": [], "facts": [], "images": [], "videos": [],
            "places": [], "confidence": "HIGH", "requires_review": False,
            "routing": {}, "trace_id": "t", "latency_ms": 1,
        }


class _LegacyPipe:
    """Pre-training custom pipeline: it has no training-context keyword."""

    async def run(self, query, session_id, as_of, history, *, project_key, device_id, on_event):
        return {
            "answer": "legacy answer", "sources": [], "facts": [], "images": [], "videos": [],
            "places": [], "confidence": "HIGH", "requires_review": False,
            "routing": {}, "trace_id": "legacy", "latency_ms": 1,
        }


class _ModernTrainingPipe:
    """Training-aware pipeline double that records the optional context field."""

    def __init__(self) -> None:
        self.training_context_project_key = None

    async def run(
        self, query, session_id, as_of, history, *, project_key, device_id, on_event,
        training_context_project_key=None,
    ):
        self.training_context_project_key = training_context_project_key
        return {
            "answer": "training answer", "sources": [], "facts": [], "images": [], "videos": [],
            "places": [], "confidence": "HIGH", "requires_review": False,
            "routing": {}, "trace_id": "training", "latency_ms": 1,
        }


class _FakeRequest:
    """Starlette Request stand-in exposing is_disconnected with call control.

    disconnected=True: every call reports the client is gone.
    disconnect_after=N: the first N calls report connected, later calls report
    the disconnect — lets the pipe settle before the teardown sees the drop.
    """

    def __init__(self, *, disconnected: bool = False, disconnect_after: int = 0) -> None:
        self._always_disconnected = disconnected
        self._disconnect_after = disconnect_after
        self._calls = 0

    async def is_disconnected(self) -> bool:
        self._calls += 1
        if self._always_disconnected:
            return True
        return self._disconnect_after and self._calls > self._disconnect_after


@pytest.mark.asyncio
async def test_sse_client_disconnect_refunds_reserved_turn_once():
    """A client that drops mid-stream must get the reserved anonymous turn back
    exactly once (spec §4 R1) — the disconnect path never leaks the slot."""
    gate = _RecordingGate()
    frames = []
    async for frame in _sse_stream(
        _StalledPipe(), QueryRequest(query="hello"), None, "camellia", None,
        gate, _ReservedContext(), request=_FakeRequest(disconnected=True),
    ):
        frames.append(frame)
    assert frames[0].startswith("event: ack")
    assert gate.refund_calls == 1
    assert gate.finalize_calls == 0


@pytest.mark.asyncio
async def test_sse_deadline_cut_refunds_reserved_turn_once():
    """A stalled stream cut by the total deadline refunds the reserved turn once
    and still ships the terminal STREAM_TIMEOUT error + done frames."""
    gate = _RecordingGate()
    frames = []
    async for frame in _sse_stream(
        _StalledPipe(), QueryRequest(query="hello"), None, "camellia", None,
        gate, _ReservedContext(), total_timeout_s=0.5,
    ):
        frames.append(frame)
    assert any("STREAM_TIMEOUT" in frame for frame in frames)
    assert frames[-1].startswith("event: done")
    assert gate.refund_calls == 1
    assert gate.finalize_calls == 0


@pytest.mark.asyncio
async def test_sse_crash_refunds_exactly_once_even_with_teardown_disconnect():
    """run_pipe's own refund (crash path) plus a disconnect during teardown must
    still yield exactly one refund — the settlement guard blocks the double."""
    gate = _RecordingGate()
    frames = []
    async for frame in _sse_stream(
        _CrashingPipe(), QueryRequest(query="hello"), None, "camellia", None,
        gate, _ReservedContext(), request=_FakeRequest(disconnect_after=1),
    ):
        frames.append(frame)
    assert frames[0].startswith("event: ack")
    assert any(frame.startswith("event: error") and "INTERNAL" in frame for frame in frames)
    assert frames[-1].startswith("event: done")
    assert gate.refund_calls == 1  # refunded by run_pipe; teardown did not double it
    assert gate.finalize_calls == 0


@pytest.mark.asyncio
async def test_sse_success_finalizes_without_refund():
    """A successfully completed answer finalizes the reservation and never
    refunds it — even when the client drops right after the done frame."""
    gate = _RecordingGate()
    frames = []
    async for frame in _sse_stream(
        _QuickPipe(), QueryRequest(query="hello"), None, "camellia", None,
        gate, _ReservedContext(), request=_FakeRequest(disconnect_after=1),
    ):
        frames.append(frame)
        if frame.startswith("event: done"):
            break
    assert frames[-1].startswith("event: done")
    assert gate.finalize_calls == 1
    assert gate.refund_calls == 0


@pytest.mark.asyncio
async def test_sse_staff_timeout_refund_and_finalize_noop():
    """Sales staff context carries no reservation: a deadline cut must neither
    refund nor finalize anything (unlimited staff leaves no quota writes)."""
    from api.application.services.query_quota_gate import AuthenticatedTurnContext

    gate = _RecordingGate()
    frames = []
    async for frame in _sse_stream(
        _StalledPipe(), QueryRequest(query="hello"), None, "camellia", None,
        gate, AuthenticatedTurnContext(role_claim="sales"), total_timeout_s=0.5,
    ):
        frames.append(frame)
    assert any("STREAM_TIMEOUT" in frame for frame in frames)
    assert frames[-1].startswith("event: done")
    assert gate.refund_calls == 0
    assert gate.finalize_calls == 0


@pytest.mark.asyncio
async def test_sse_legacy_pipeline_without_training_context_streams_done_answer():
    """Optional training metadata must not break existing custom pipelines."""
    frames = []
    async for frame in _sse_stream(
        _LegacyPipe(), QueryRequest(query="hello"), None, "camellia", None,
        _Gate(), _Context(),
    ):
        frames.append(frame)
    assert frames[-1].startswith("event: done")
    done = json.loads(frames[-1].split("data: ", 1)[1])
    assert done["answer"] == "legacy answer"
    assert not any(frame.startswith("event: error") for frame in frames)


@pytest.mark.asyncio
async def test_sse_training_pipeline_receives_context_project_key():
    """The new field still reaches pipelines that explicitly support training."""
    pipe = _ModernTrainingPipe()
    request = QueryRequest(
        query="coaching", answer_mode="training", context={"project_key": "camellia"}
    )
    frames = []
    async for frame in _sse_stream(
        pipe, request, None, "_training", None, _Gate(), _Context(),
        training_context_key="camellia",
    ):
        frames.append(frame)
    assert pipe.training_context_project_key == "camellia"
    assert frames[-1].startswith("event: done")
    done = json.loads(frames[-1].split("data: ", 1)[1])
    assert done["answer"] == "training answer"
    assert done["context"] == {"project_key": "camellia"}
