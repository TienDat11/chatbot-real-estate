"""SSE stream termination + event-order regression tests for GA-06.

These pin the P0 deadlock fix (`_safe_execute` enqueues `_Q_EXEC_DONE` in its
`finally` so the drain loop always terminates even when the pipeline never fires
its `on_event` callback) and the documented ordering invariant
(ack -> project_context -> routing -> pipeline events -> token -> done).

Driven directly against the pure `_stream_assistant` generator: no FastAPI app,
no Postgres, no real HTTP. The core, request, and quota gate are injected fakes.
The fakes provide `prepare_turn`/`finalize_turn`/`refund_turn` on the quota gate
(the real resolved path calls all three), and `prepare_turn`/`execute_prepared_turn`
on the core; there is no rag/sql/geo leg fake, so a resolved turn that never
emits events proves the sentinel path.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from api.application.services.global_assistant_service import (
    GlobalAssistantInput,
    GlobalAssistantOutcome,
    PreparedTurn,
)
from api.application.services.global_project_resolution import (
    ProjectResolution,
    ProjectResolutionSource,
    ProjectResolutionStatus,
)
from api.interfaces.api.assistant_routes import _make_callback, _stream_assistant
from tests.test_final_answer_integrity import (  # noqa: E402
    DESCRIPTIVE,
    _Harness,
    _long_descriptive,
    _build_core,
    sha,
)
from tests.test_global_assistant_service import FakeHistory


class _FakeRequest:
    """Minimal ASGI request stand-in for the stream generator."""

    def __init__(self, bearer: str | None = None, ip: str = "127.0.0.53") -> None:
        self.headers = {"authorization": (bearer or "")}
        # get_client_ip_address(request) reads request.client.host.
        self.client = type("_C", (), {"host": ip})()

    async def is_disconnected(self) -> bool:
        return False


class _StubCore:
    """Core seam returning a precomputed PreparedTurn + outcome.

    `_stream_assistant` never invokes `pipeline_factory` directly — the real core
    does, but this stub bypasses the pipeline entirely. The SSE layer's contract is
    that the route drains queue events and settles quota/finalizes on whatever the
    core returns; proving the pipeline is not constructed for ambiguous/unresolved
    turns is owned by tests/test_global_assistant_service.py against the REAL
    GlobalAssistantService. This stub therefore exposes only the methods the
    route actually calls (prepare_turn, execute_prepared_turn).
    """

    def __init__(
        self, prepared: PreparedTurn, outcome: GlobalAssistantOutcome
    ) -> None:
        self._prepared = prepared
        self._outcome = outcome
        # Counts the core's execute_prepared_turn entry (invoked on unresolved
        # for clarification too). This is the only production call the SSE route
        # drives against the core; pipeline_factory is not invoked here.
        self.execute_called = 0

    async def prepare_turn(self, request: GlobalAssistantInput) -> PreparedTurn:
        return self._prepared

    async def execute_prepared_turn(self, prepared: PreparedTurn) -> GlobalAssistantOutcome:
        self.execute_called += 1
        return self._outcome


class _ReservedTurnContext:
    """Stand-in for Anonymous/CustomerTurnContext: finite reservation present.

    ``_reservation_managed`` keys on ``reserved_snapshot`` — the same attribute
    the real gate dereferences in ``finalize_turn``/``refund_turn``.
    """

    def __init__(self, project_key: str = "soleil") -> None:
        self.identity_key = "anon:test"
        self.project_key = project_key
        self.reserved_snapshot = object()


class _UnmanagedTurnContext:
    """Stand-in for AuthenticatedTurnContext: unlimited, no reservation.

    The real class exposes ``role_claim``/``quota_payload`` but NO
    ``reserved_snapshot``, so any settle call raises ``AttributeError``.
    """

    role_claim = "staff"


class _RecordingQuotaGate:
    """Full three-method quota seam (prepare/finalize/refund)."""

    def __init__(self, turn_context=None) -> None:
        self.prepared = 0
        self.finalized = 0
        self.refunded = 0
        self._turn_context = (
            turn_context if turn_context is not None else _ReservedTurnContext()
        )

    async def prepare_turn(self, **kwargs):
        self.prepared += 1
        return self._turn_context

    async def finalize_turn(self, context=None):
        self.finalized += 1

    async def refund_turn(self, context=None):
        self.refunded += 1


def _parse(frames: list[str]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for f in frames:
        assert f.endswith("\n\n"), f
        body = f[:-2]
        head, _, payload = body.partition("\n")
        assert head.startswith("event: "), head
        event = head.split("event: ", 1)[1]
        text = payload
        if text.startswith("data: "):
            text = text[len("data: "):]
        out.append((event, json.loads(text)))
    return out


def _resolved_turn(*, candidates=(), project_key="soleil") -> ProjectResolution:
    return ProjectResolution(
        status=ProjectResolutionStatus.RESOLVED,
        project_key=project_key,
        source=ProjectResolutionSource.SESSION_CONTEXT,
        candidates=tuple(candidates),
    )


def _unresolved_turn() -> ProjectResolution:
    return ProjectResolution(
        status=ProjectResolutionStatus.UNRESOLVED,
        project_key=None,
        source=ProjectResolutionSource.NONE,
        candidates=(),
    )


def _ambiguous_turn() -> ProjectResolution:
    return ProjectResolution(
        status=ProjectResolutionStatus.AMBIGUOUS,
        project_key=None,
        source=ProjectResolutionSource.EXPLICIT_QUERY,
        candidates=("camellia", "soleil"),
    )


def _prepared(resolution: ProjectResolution, known_projects=None) -> PreparedTurn:
    return PreparedTurn(
        request=GlobalAssistantInput(query="giá?", session_id="s", identity_key="i"),
        resolution=resolution,
        known_projects=list(known_projects or []),
        messages=[],
        history=[],
        replay=None,
    )


async def _drain(gen) -> list[str]:
    return [f async for f in gen]


@pytest.mark.asyncio
async def test_unresolved_stream_emits_ack_project_context_done_no_token():
    """Unresolved path: ack -> project_context -> done; NO rag pipeline legs,
    NO quota reservation, NO token frame (spec pins each path's ordering).

    The picker options surface on BOTH the project_context frame and the
    terminal done payload via the single-source `clarification_candidates`
    accessor (the UNRESOLVED catalogue fallback).
    """
    known = [("soleil", "Soleil")]
    core = _StubCore(
        prepared=_prepared(_unresolved_turn(), known_projects=known),
        outcome=GlobalAssistantOutcome(
            answer="Bạn muốn hỏi dự án nào? Vui lòng chọn một trong các dự án: soleil",
            resolution_status="unresolved",
            resolution_source="none",
            candidates=("soleil",),
            meta={"candidates": ["soleil"]},
        ),
    )
    gate = _RecordingQuotaGate()
    frames = await _drain(
        _stream_assistant(
            request=_FakeRequest(),
            core=core,
            input_=core._prepared.request,
            queue=asyncio.Queue(),
            quota_gate=gate,
            x_anon_token=None,
            staff_principal=None,
        )
    )
    events = [e for e, _ in _parse(frames)]
    # Exact unresolved ordering: ack -> project_context -> done (no token).
    assert events == ["ack", "project_context", "done"], events
    # Picker options come from the LIVE catalogue (clarification_candidates
    # accessor), surfaced on both frames — not the resolver's empty match list.
    project_context = _parse(frames)[1][1]
    assert project_context["candidates"] == ["soleil"], project_context
    done = _parse(frames)[-1][1]
    assert done["candidates"] == ["soleil"], done
    assert done["resolution_status"] == "unresolved"
    # The route owns: clarification entry (core.execute_prepared_turn for the
    # unresolved prompt), exact ordering ack -> project_context -> done, NO token
    # frame, NO quota reservation/refund. The retrieval pipeline not running is
    # proven against the REAL core in test_global_assistant_service.py, not here.
    assert core.execute_called == 1
    assert gate.prepared == 0
    assert gate.finalized == 0
    assert gate.refunded == 0


@pytest.mark.asyncio
async def test_resolved_zero_pipeline_events_unresolved_deadlock_sentinel_terminates():
    """P0 deadlock guard: a resolved turn whose pipeline emits ZERO SSE events
    must still terminate via the `_Q_EXEC_DONE` sentinel. Before the fix the
    drain loop would `await queue.get()` forever.

    Note: the stub core emits no on_event callbacks, so this is the empty-event
    case that previously hung. The resolved path's exact ordering is asserted by
    index: ack < project_context < token < done.
    """
    core = _StubCore(
        prepared=_prepared(_resolved_turn(), known_projects=[("soleil", "Soleil")]),
        outcome=GlobalAssistantOutcome(
            answer="42 tỷ.",
            resolution_status="resolved",
            resolution_source="session_context",
            project_key="soleil",
            meta={"routing": {"needs_rag": False}, "sources": []},
            rag_ran=True,
        ),
    )
    gate = _RecordingQuotaGate()
    frames = await _drain(
        _stream_assistant(
            request=_FakeRequest(),
            core=core,
            input_=core._prepared.request,
            queue=asyncio.Queue(),
            quota_gate=gate,
            x_anon_token=None,
            staff_principal=None,
        )
    )
    events = [e for e, _ in _parse(frames)]
    # Exact resolved ordering: ack -> project_context -> token -> done.
    assert events == ["ack", "project_context", "token", "done"], events
    assert core.execute_called == 1  # core execute_prepared_turn invoked exactly once
    # Quota reserved after resolution (spec §5.3) and finalized on success.
    assert gate.prepared == 1
    assert gate.finalized == 1
    assert gate.refunded == 0
    idx = {e: i for i, (e, _) in enumerate(_parse(frames))}
    assert idx["ack"] < idx["project_context"] < idx["token"] < idx["done"]


@pytest.mark.asyncio
async def test_ambiguous_stream_emits_ack_project_context_done_no_token():
    """AMBIGUOUS path: ack -> project_context -> done; NO rag pipeline legs,
    NO quota reservation, NO token frame. Mirrors the unresolved ordering but
    surfaces the resolver's disambiguation candidate set on project_context
    and done.

    The picker options come from the resolver's matched keys (the single-source
    clarification_candidates accessor for AMBIGUOUS), surfacing the same set on
    both the project_context frame and the terminal done payload.
    """
    known = [("camellia", "Camellia"), ("soleil", "Soleil")]
    core = _StubCore(
        prepared=_prepared(_ambiguous_turn(), known_projects=known),
        outcome=GlobalAssistantOutcome(
            answer="Bạn muốn hỏi dự án nào? Vui lòng chọn một trong các dự án: camellia, soleil",
            resolution_status="ambiguous",
            resolution_source="explicit_query",
            candidates=("camellia", "soleil"),
            meta={"candidates": ["camellia", "soleil"]},
        ),
    )
    gate = _RecordingQuotaGate()
    frames = await _drain(
        _stream_assistant(
            request=_FakeRequest(),
            core=core,
            input_=core._prepared.request,
            queue=asyncio.Queue(),
            quota_gate=gate,
            x_anon_token=None,
            staff_principal=None,
        )
    )
    events = [e for e, _ in _parse(frames)]
    # Exact ambiguous ordering: ack -> project_context -> done (no token).
    assert events == ["ack", "project_context", "done"], events
    project_context = _parse(frames)[1][1]
    assert project_context["candidates"] == ["camellia", "soleil"], project_context
    done = _parse(frames)[-1][1]
    assert done["candidates"] == ["camellia", "soleil"], done
    assert done["resolution_status"] == "ambiguous"
    # The route owns: exact ordering ack -> project_context -> done, NO token
    # frame, NO quota reservation/refund. The retrieval pipeline not running is
    # proven against the REAL core in test_global_assistant_service.py, not here.
    assert core.execute_called == 1
    assert gate.prepared == 0
    assert gate.finalized == 0
    assert gate.refunded == 0


class _HangingCore(_StubCore):
    """Core whose pipeline never finishes, so only teardown can end the turn."""

    def __init__(self, prepared: PreparedTurn) -> None:
        super().__init__(prepared=prepared, outcome=GlobalAssistantOutcome(
            answer="never",
            resolution_status="resolved",
            resolution_source="session_context",
            project_key="soleil",
        ))
        self.execute_started = 0
        self.execute_cancelled = 0

    async def execute_prepared_turn(self, prepared: PreparedTurn) -> GlobalAssistantOutcome:
        self.execute_started += 1
        try:
            await asyncio.Event().wait()  # never set: only cancellation ends this
        except asyncio.CancelledError:
            self.execute_cancelled += 1
            raise
        raise AssertionError("hanging core must not return")


def _stream(core, gate, prepared: PreparedTurn):
    return _stream_assistant(
        request=_FakeRequest(),
        core=core,
        input_=prepared.request,
        queue=asyncio.Queue(),
        quota_gate=gate,
        x_anon_token=None,
        staff_principal=None,
    )


@pytest.mark.asyncio
async def test_outer_cancellation_cancels_pipeline_and_refunds_once():
    """Client disconnect / response-task cancellation: the generator's teardown
    cancels the still-running pipeline and refunds the reservation exactly once
    (never finalizes — the answer was never delivered)."""
    prepared = _prepared(_resolved_turn(), known_projects=[("soleil", "Soleil")])
    core = _HangingCore(prepared)
    gate = _RecordingQuotaGate()
    gen = _stream(core, gate, prepared)

    assert (await gen.__anext__()).startswith("event: ack")
    assert (await gen.__anext__()).startswith("event: project_context")

    # Third pull enters phase 3/4: quota reserved, pipeline task started, then
    # the drain loop parks on the empty queue.
    pending = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    assert gate.prepared == 1, "reservation must be taken before the pipeline"
    assert core.execute_started == 1, "pipeline task must be running"

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # Teardown: pipeline cancelled and the reservation refunded exactly once.
    assert core.execute_cancelled == 1, "teardown must cancel the pipeline task"
    assert gate.refunded == 1, "unsettled reservation must be refunded once"
    assert gate.finalized == 0, "a cancelled turn must never be finalized"


@pytest.mark.asyncio
async def test_unmanaged_staff_turn_is_never_settled():
    """Staff turns carry no reservation: the route must skip finalize/refund
    (calling either would raise AttributeError and 500 a verified caller)."""
    prepared = _prepared(_resolved_turn(), known_projects=[("soleil", "Soleil")])
    core = _StubCore(
        prepared=prepared,
        outcome=GlobalAssistantOutcome(
            answer="42 tỷ.",
            resolution_status="resolved",
            resolution_source="session_context",
            project_key="soleil",
        ),
    )
    gate = _RecordingQuotaGate(turn_context=_UnmanagedTurnContext())
    frames = await _drain(_stream(core, gate, prepared))

    events = [e for e, _ in _parse(frames)]
    assert events == ["ack", "project_context", "token", "done"], events
    assert gate.prepared == 1
    assert gate.finalized == 0, "unlimited turn has nothing to finalize"
    assert gate.refunded == 0, "unlimited turn has nothing to refund"


@pytest.mark.asyncio
async def test_resolved_failure_refunds_and_terminates_with_error_done():
    """Pipeline failure: error -> done, reservation refunded once, never
    finalized, and the stream still terminates (no deadlock)."""
    prepared = _prepared(_resolved_turn(), known_projects=[("soleil", "Soleil")])
    core = _StubCore(prepared=prepared, outcome=RuntimeError("boom"))
    gate = _RecordingQuotaGate()
    frames = await _drain(_stream(core, gate, prepared))

    parsed = _parse(frames)
    events = [e for e, _ in parsed]
    assert events == ["ack", "project_context", "error", "done"], events
    assert parsed[2][1]["code"] == "INTERNAL"
    assert gate.prepared == 1
    assert gate.refunded == 1, "failed turn must refund its reservation"


class _HungRefundGate(_RecordingQuotaGate):
    """Gate whose refund_turn hangs on a never-set asyncio.Event, simulating a
    hung DB write (`refund_turn_atomically` awaiting an unresponsive store).

    Records that refund_turn was entered so the regression can prove the teardown
    ATTEMPTED the refund even when the generator is cancelled mid-teardown.
    """

    def __init__(self, turn_context=None) -> None:
        super().__init__(turn_context=turn_context)
        self.refund_entered = False
        self.refund_cancelled = False

    async def refund_turn(self, context=None):
        self.refund_entered = True
        self.refunded += 1
        try:
            # Never set: only cancellation ends this await.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.refund_cancelled = True
            raise

    async def finalize_turn(self, context=None):
        self.finalized += 1


@pytest.mark.asyncio
async def test_cancellation_during_hung_refund_terminates_boundedly():
    """Cancellation-safety regression for the SSE teardown (item 11).

    When the generator is cancelled while `_settle_turn` is awaiting a hung
    ``refund_turn`` (a real DB write that never returns), the teardown must:
      (a) terminate — the generator must not hang (bounded wait_for around the
          shielded refund),
      (b) attempt the refund — refund_turn was entered,
      (c) NEVER finalize — the turn was never delivered (finalized == 0),
      (d) not rely on wall-clock assertions beyond the existing pattern.

    Pre-fix the teardown did ``turn_settled = True`` BEFORE ``await
    quota_gate.refund_turn(...)`` and suppressed only ``Exception``;
    ``asyncio.CancelledError`` (a BaseException) escaped mid-refund, leaving a
    permanently leaked reservation with no reaper sweep.
    """
    prepared = _prepared(_resolved_turn(), known_projects=[("soleil", "Soleil")])
    core = _HangingCore(prepared)
    gate = _HungRefundGate()
    gen = _stream(core, gate, prepared)

    assert (await gen.__anext__()).startswith("event: ack")
    assert (await gen.__anext__()).startswith("event: project_context")

    # Phase 3/4: quota reserved, pipeline task started, drain loop parks.
    pending = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.05)
    assert gate.prepared == 1, "reservation must be taken before the pipeline"
    assert core.execute_started == 1, "pipeline task must be running"

    # Cancel the in-flight __anext__: the teardown must run the refund attempt
    # (which hangs) and terminate within the bounded wait_for, not propagate the
    # cancellation past the suppress boundary.
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    # (a) Generator terminated: the drain task is done (cancelled) and the
    #      teardown returned. A small grace sleep lets the finally-bounded
    #      refund task settle without relying on exact wall-clock math.
    await asyncio.sleep(0.1)
    assert pending.done()

    # (b) The refund was attempted even under cancellation.
    assert gate.refund_entered, "teardown must attempt refund even if it hangs"

    # (c) A cancelled turn must never be finalized (answer never delivered).
    assert gate.finalized == 0, "cancelled turn must never be finalized"

    # (d) The bounded teardown surfaced exactly one refund attempt.
    assert gate.refunded == 1


@pytest.mark.asyncio
async def test_real_core_bounded_stream_done_and_persisted_bytes_agree(monkeypatch):
    """P0-05: the SSE token, the `done` payload, the persisted assistant row and
    the audit hash are the SAME bytes for a real core + real facade.

    The bound is a GA input, not a public HTTP field: it travels core -> facade
    -> workflow finalization, so it is applied before the answer is tokenized,
    hashed and audited. Only the LLM provider, the registry pool, history and
    quota are fakes.
    """
    draft = _long_descriptive()
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=draft)
    history = FakeHistory()
    core = _RealCore(harness, history)
    gate = _RecordingQuotaGate(turn_context=_ReservedTurnContext("camellia"))
    queue: asyncio.Queue = asyncio.Queue()
    input_ = GlobalAssistantInput(
        query="Camellia tiện ích", session_id="p05-stream", identity_key="anon:t",
        max_visible_chars=2000, on_event=_make_callback(queue),
    )

    frames = await _drain(_stream_assistant(
        request=_FakeRequest(), core=core, input_=input_, queue=queue,
        quota_gate=gate, x_anon_token=None, staff_principal=None,
    ))

    parsed = _parse(frames)
    events = [name for name, _ in parsed]
    assert events[0] == "ack" and events[1] == "project_context"
    assert events[-1] == "done"
    assert parsed[-1][1]["resolution_status"] == "resolved"

    streamed = "".join(d.get("text", "") for name, d in parsed if name == "token")
    done_answer = parsed[-1][1]["answer"]
    persisted = history.appended[0]["assistant_content"]
    expected = streamed.encode("utf-8")

    # One string, four surfaces.
    # The budget counts characters, not UTF-8 bytes: this Vietnamese answer is
    # ~1.9k chars and 2.7k bytes, and every surface holds the same string.
    assert len(streamed) <= 2000
    assert len(expected) > len(streamed)
    assert expected.startswith("Căn hộ tại Camellia.".encode("utf-8"))
    assert done_answer.encode("utf-8") == expected
    assert persisted.encode("utf-8") == expected
    assert harness.audits[-1]["answer_hash"] == sha(streamed)
    # The audit row is keyed by the same trace the `done` payload reports.
    assert harness.audits[-1]["trace_id"] == parsed[-1][1]["trace_id"]
    # The turn was delivered, so the reservation is finalized, never refunded.
    assert gate.prepared == 1 and gate.finalized == 1 and gate.refunded == 0


@pytest.mark.asyncio
async def test_real_core_unbounded_stream_keeps_the_whole_answer(monkeypatch):
    """No budget means no cut: the whole answer reaches every surface."""
    harness = _Harness(monkeypatch)
    harness.install(query="Camellia tiện ích", draft=DESCRIPTIVE)
    history = FakeHistory()
    core = _RealCore(harness, history)
    queue: asyncio.Queue = asyncio.Queue()

    frames = await _drain(_stream_assistant(
        request=_FakeRequest(), core=core, queue=queue,
        input_=GlobalAssistantInput(
            query="Camellia tiện ích", session_id="p05-unbounded", identity_key="anon:t",
            on_event=_make_callback(queue),
        ),
        quota_gate=_RecordingQuotaGate(turn_context=_ReservedTurnContext("camellia")),
        x_anon_token=None, staff_principal=None,
    ))

    parsed = _parse(frames)
    streamed = "".join(d.get("text", "") for n, d in parsed if n == "token")
    assert streamed == DESCRIPTIVE
    assert parsed[-1][1]["answer"].encode("utf-8") == DESCRIPTIVE.encode("utf-8")
    assert history.appended[0]["assistant_content"] == DESCRIPTIVE
    assert harness.audits[-1]["answer_hash"] == sha(DESCRIPTIVE)

class _RealCore:
    """The REAL ``GlobalAssistantService`` over a fake history and a real facade.

    Everything between the HTTP frame and the audit row is production code:
    resolution, the conv facade, the workflow finalization gate, the core
    watchdog and the history write.
    """

    def __init__(self, harness: _Harness, history) -> None:
        self._service = _build_core(harness, history)

    async def prepare_turn(self, request):
        return await self._service.prepare_turn(request)

    async def execute_prepared_turn(self, prepared):
        return await self._service.execute_prepared_turn(prepared)
