"""Additive Global Assistant HTTP adapter (GA-BE-06).

Thin FastAPI-only layer: it derives the verified anonymous identity from the
signed ``X-Anon-Token`` header (never from a request body), builds a
``GlobalAssistantInput``, and drives the transport-neutral core. It does NOT
reimplement resolution/history/RAG — those live in the application service.

The private ``source_event_key`` is intentionally NOT accepted from the public
contract; transports that need idempotency replay the caller's own session
through a separate, non-HTTP channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.application.services.anon_identity import AnonymousIdentityService
from api.application.services.chat_history_service import ChatHistoryService
from api.application.services.global_assistant_service import (
    GlobalAssistantInput,
    GlobalAssistantOutcome,
    GlobalAssistantService,
    PreparedTurn,
)
from api.application.services.query_quota_gate import (
    QueryQuotaBlockedError,
    build_query_quota_gate,
    build_quota_blocked_json_body,
)
from api.domain.value_objects.constants import MAX_QUERY_LENGTH
from api.infrastructure.adapters.postgres_chat_history import repository as chat_history_repository
from api.interfaces.api.anon_routes import get_anonymous_identity_service, get_client_ip_address
from api.interfaces.api.deps import AuthenticatedPrincipal, optional_sales_or_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/assistant", tags=["assistant"])

_SSE_RESPONSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# Queue sentinel: pipeline execution completed (success or error).
_Q_EXEC_DONE = object()


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _clean_project_key(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return value


def _reservation_managed(turn_context: object) -> bool:
    """True when the turn carries a finite reservation the route must settle.

    Staff (``AuthenticatedTurnContext``) and unmanaged turns are unlimited and
    never reserve a turn — they expose no ``reserved_snapshot`` — so the
    finalize/refund calls MUST be skipped for them: either call would raise
    ``AttributeError`` and turn a verified staff request into a 500. Legacy
    ``/query`` guards its own calls the same way.
    """
    return getattr(turn_context, "reserved_snapshot", None) is not None


class AssistantQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    selected_project_key: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    route_project_key: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")


class AssistantQueryResponse(BaseModel):
    answer: str
    resolution_status: str
    resolution_source: str
    candidates: list[str] = Field(default_factory=list)
    project_key: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    rag_ran: bool = False
    replayed: bool = False
    trace_id: str = ""
    latency_ms: int = 0


def _service() -> GlobalAssistantService:
    """Build the transport-neutral core.

    The core takes a ChatHistoryService (not the raw repository): that service
    exposes global_session/global_messages/persist_global_turn, which the raw
    Postgres repository does not.
    """
    from api.application.pipelines.conv_workflow import (  # noqa: PLC0415
        RagQueryPipelineConv,
    )

    history = ChatHistoryService(chat_history_repository)
    return GlobalAssistantService(
        history=history,
        pipeline_factory=RagQueryPipelineConv,
    )


def _resolve_identity(
    request: Request,
    x_device_id: str | None,
    x_anon_token: str | None,
    identity_service: AnonymousIdentityService | None,
    staff_principal: AuthenticatedPrincipal | None,
) -> tuple[str, str, str | None]:
    """Return (session_id, identity_key, device_id) from verified context.

    Staff callers authenticate via Bearer (principal provided); they must still
    present an anonymous device handle and an explicit session id. Anonymous
    callers must present a signed anon token — the identity_key is derived,
    never client-supplied. The session id is a routing handle, never a
    credential.
    """
    if staff_principal is not None:
        device_id = x_device_id
        if not device_id:
            raise HTTPException(status_code=400, detail="X-Device-Id is required")
        session_id = request.headers.get("x-session-id") or ""
        if not session_id:
            raise HTTPException(status_code=400, detail="X-Session-Id is required for staff")
        return session_id, staff_principal.firebase_uid, device_id

    if not x_anon_token or not identity_service or not x_device_id:
        raise HTTPException(status_code=401, detail="Signed anonymous identity is required")
    claims = identity_service.verify_token(x_anon_token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Invalid anonymous identity")
    session_id = request.headers.get("x-session-id") or claims.subject
    return session_id, claims.subject, x_device_id


@router.post("/query")
async def assistant_query_json(
    request: Request,
    req: AssistantQueryRequest,
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    x_anon_token: str | None = Header(default=None, alias="X-Anon-Token"),
    identity_service: AnonymousIdentityService | None = Depends(get_anonymous_identity_service),  # noqa: B008
    staff_principal: AuthenticatedPrincipal | None = Depends(optional_sales_or_admin),  # noqa: B008
) -> AssistantQueryResponse:
    """Non-streaming JSON path: prepare → quota → execute → finalize/refund."""
    session_id, identity_key, device_id = _resolve_identity(
        request, x_device_id, x_anon_token, identity_service, staff_principal
    )

    input_ = GlobalAssistantInput(
        query=req.query,
        session_id=session_id,
        identity_key=identity_key,
        device_id=device_id,
        route_project_key=_clean_project_key(req.route_project_key),
        selected_project_key=_clean_project_key(req.selected_project_key),
    )

    core = _service()
    quota_gate = build_query_quota_gate()

    prepared = await core.prepare_turn(input_)

    # Replay: no pipeline, no quota — return directly.
    if prepared.replay is not None:
        return _to_response(prepared.replay)

    # Unresolved: no pipeline, no quota — just clarification.
    if prepared.resolution.project_key is None:
        outcome = await core.execute_prepared_turn(prepared)
        return _to_response(outcome)

    # Reserve quota strictly AFTER resolution (spec §5.3).
    bearer_id_token = (
        request.headers.get("authorization") or ""
    )
    bearer_id_token = (
        bearer_id_token[7:].strip()
        if bearer_id_token.lower().startswith("bearer ")
        else None
    )
    try:
        turn_context = await quota_gate.prepare_turn(
            presented_anon_token=x_anon_token,
            bearer_id_token=bearer_id_token,
            client_ip_address=get_client_ip_address(request),
            project_key=prepared.resolution.project_key,
        )
    except QueryQuotaBlockedError as blocked:
        return JSONResponse(
            status_code=blocked.status_code,
            content=build_quota_blocked_json_body(blocked),
        )

    try:
        outcome = await core.execute_prepared_turn(prepared)
    except Exception:  # noqa: BLE001 — refund quota on any pipeline/persistence failure
        if _reservation_managed(turn_context):
            await quota_gate.refund_turn(turn_context)
        raise

    # Staff (authenticated) and unmanaged turns carry no reservation: there is
    # nothing to settle and calling finalize would raise AttributeError.
    if _reservation_managed(turn_context):
        await quota_gate.finalize_turn(turn_context)
    return _to_response(outcome)


@router.post("/query/stream")
async def assistant_query_stream(
    request: Request,
    req: AssistantQueryRequest,
    x_device_id: str | None = Header(default=None, alias="X-Device-Id"),
    x_anon_token: str | None = Header(default=None, alias="X-Anon-Token"),
    identity_service: AnonymousIdentityService | None = Depends(get_anonymous_identity_service),  # noqa: B008
    staff_principal: AuthenticatedPrincipal | None = Depends(optional_sales_or_admin),  # noqa: B008
    quota_gate: build_query_quota_gate = Depends(build_query_quota_gate),  # noqa: B008
) -> StreamingResponse:
    """SSE streaming path: prepare → quota → execute with event streaming.

    Emits the required event family in order:
    ack -> project_context -> routing -> sources/facts/places/images -> token -> done.
    On failure: error -> done (with quota refunded).
    """
    session_id, identity_key, device_id = _resolve_identity(
        request, x_device_id, x_anon_token, identity_service, staff_principal
    )

    # Per-request queue: the on_event callback enqueues pipeline events, and the
    # background task runner drains and yields them as SSE frames.
    queue: asyncio.Queue[tuple[str, dict] | object] = asyncio.Queue()
    input_ = GlobalAssistantInput(
        query=req.query,
        session_id=session_id,
        identity_key=identity_key,
        device_id=device_id,
        route_project_key=_clean_project_key(req.route_project_key),
        selected_project_key=_clean_project_key(req.selected_project_key),
        on_event=_make_callback(queue),
    )

    return StreamingResponse(
        _stream_assistant(
            request=request,
            core=_service(),
            input_=input_,
            queue=queue,
            quota_gate=quota_gate,
            x_anon_token=x_anon_token,
            staff_principal=staff_principal,
        ),
        media_type="text/event-stream",
        headers=_SSE_RESPONSE_HEADERS,
    )


def _make_callback(queue: asyncio.Queue) -> Callable[[str, dict], Awaitable[None] | None]:
    """Create a per-stream callback that enqueues (event, data) pairs.

    The callback is invoked synchronously by the pipeline's internal emit step.
    Async coroutines are awaited inline so the queue receives every event
    before the pipeline advances to the next step.
    """
    async def _callback(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    return _callback




async def _stream_assistant(
    request: Request,
    core: GlobalAssistantService,
    input_: GlobalAssistantInput,
    queue: asyncio.Queue,
    quota_gate,
    x_anon_token: str | None,
    staff_principal: AuthenticatedPrincipal | None,
) -> AsyncIterator[str]:
    """Drive the core's prepare/execute split as an SSE stream.

    Ordering invariant: ack -> project_context -> routing -> (sources/facts/
    places/images from pipeline) -> token -> done.
    On failure: error -> done (quota refunded).
    """

    # Phase 1 — resolve + replay check (no pipeline, no quota).
    prepared = await core.prepare_turn(input_)

    if prepared.replay is not None:
        yield _frame("ack", {"session_id": prepared.request.session_id})
        yield _frame(
            "project_context",
            {
                "project_key": prepared.replay.project_key,
                "resolution_status": prepared.replay.resolution_status,
            },
        )
        yield _frame("done", prepared.replay.as_dict())
        return

    # Phase 2 — ack + project_context MUST emit before token/done.
    yield _frame("ack", {"session_id": prepared.request.session_id})
    yield _frame(
        "project_context",
        {
            "project_key": prepared.resolution.project_key,
            "resolution_status": prepared.resolution.status.value,
            "resolution_source": prepared.resolution.source.value,
            "candidates": list(prepared.clarification_candidates()),
        },
    )

    # Unresolved: no pipeline, no quota — clarification + done.
    if prepared.resolution.project_key is None:
        outcome = await core.execute_prepared_turn(prepared)
        yield _frame("done", outcome.as_dict())
        return

    # Phase 3 — reserve quota strictly AFTER resolution (spec §5.3).
    bearer_id_token = (
        request.headers.get("authorization") or ""
    )
    bearer_id_token = (
        bearer_id_token[7:].strip()
        if bearer_id_token.lower().startswith("bearer ")
        else None
    )
    try:
        turn_context = await quota_gate.prepare_turn(
            presented_anon_token=x_anon_token,
            bearer_id_token=bearer_id_token,
            client_ip_address=get_client_ip_address(request),
            project_key=prepared.resolution.project_key,
        )
    except QueryQuotaBlockedError as blocked:
        yield _frame("error", _quota_to_frame(blocked))
        yield _frame("done", {})
        return

    # Phase 4 — run the pipeline in a background task; its on_event callback
    # enqueues SSE frames. We drain them as they arrive, interleaving with the
    # client disconnection guard. The reservation is settled exactly once —
    # finalize on success, refund on every other exit — by the teardown below.
    turn_settled = False

    async def _settle_turn() -> None:
        """Refund an unsettled finite reservation exactly once (spec §4 R1).

        The refund is attempted in a shielded, bounded task so a propagation of
        ``asyncio.CancelledError`` from the generator being torn down (e.g. the
        ASGI server cancelling the response task) cannot abort the refund mid-flight
        while the reservation is already marked settled. ``CancelledError`` derives
        from ``BaseException`` (not ``Exception``), so it is NOT caught by a bare
        ``contextlib.suppress(Exception)`` around the awaited refund — it would
        propagate and leave the reservation permanently leaked (there is no
        reaper/sweep). The refund is therefore driven through ``wait_for`` +
        ``shield`` and BOTH ``TimeoutError`` and ``CancelledError`` are suppressed
        here so the teardown always terminates and the reservation is always
        ATTEMPTED. ``turn_settled`` is only flipped AFTER the attempt so a
        cancelled-mid-refund turn does not silently look settled.
        """
        nonlocal turn_settled
        if turn_settled or not _reservation_managed(turn_context):
            return
        # Bounded attempt: a hung DB refund must not park the generator. The
        # existing run_task settle uses 2.0s; reuse the same order of magnitude.
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(quota_gate.refund_turn(turn_context)), timeout=2.0)
        turn_settled = True

    outcome: GlobalAssistantOutcome | None = None
    run_task = asyncio.create_task(_safe_execute(core, prepared, queue))
    try:
        while True:
            if await request.is_disconnected():
                # Client gone: the teardown cancels the pipeline and refunds.
                return

            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except TimeoutError:
                continue

            if item is _Q_EXEC_DONE:
                outcome = await run_task
                break
            event, data = item
            yield _frame(event, data)

        # Check for execution failure.
        if isinstance(outcome, Exception):
            yield _frame("error", {"code": "INTERNAL", "message": "internal error"})
            yield _frame("done", {})
            return

        if outcome is None:
            yield _frame("error", {"code": "INTERNAL", "message": "execution did not complete"})
            yield _frame("done", {})
            return

        # Phase 5 — settle the reservation for the completed run. A finalize
        # that reports False leaves the reservation for the cleanup sweep: the
        # answer was delivered, so the turn MUST never be refunded afterwards.
        if _reservation_managed(turn_context):
            await quota_gate.finalize_turn(turn_context)
        turn_settled = True

        # Terminal ordering: token -> done (project_context and routing already
        # emitted before the pipeline; pipeline events were drained above). The
        # frame carries the same `text` key as the legacy token frames so a
        # consumer that reads `data.text` never sees `undefined`.
        yield _frame("token", {"text": ""})
        yield _frame("done", outcome.as_dict())
    except Exception:  # noqa: BLE001 — error then done; teardown refunds
        logger.exception("assistant stream failed")
        yield _frame("error", {"code": "INTERNAL", "message": "internal error"})
        yield _frame("done", {})
    finally:
        # Every exit path that reached here without settling — client
        # disconnect, generator close, outer cancellation, crash — cancels and
        # awaits the pipeline task, then refunds an unsettled reservation.
        # Mirrors the legacy `/query` SSE teardown in main.py.
        if not run_task.done():
            run_task.cancel()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            # Bounded settle: a pipeline that ignores cancellation must not
            # park the generator (and the client) forever.
            await asyncio.wait_for(asyncio.shield(run_task), timeout=2.0)
        with contextlib.suppress(Exception):  # noqa: BLE001 — never mask the exit
            await _settle_turn()


async def _safe_execute(
    core: GlobalAssistantService, prepared: PreparedTurn, queue: asyncio.Queue
) -> GlobalAssistantOutcome | Exception:
    """Run the pipeline; catch exceptions and enqueue the done sentinel.

    The sentinel (``_Q_EXEC_DONE``) is always enqueued in ``finally`` so the
    drain loop in ``_stream_assistant`` cannot deadlock when the pipeline
    produces no SSE events. Any exception is returned (not raised) so the
    generator can emit ``error`` + ``done`` frames.
    """
    try:
        result = await core.execute_prepared_turn(prepared)
        return result
    except Exception as exc:  # noqa: BLE001 — propagate to stream generator
        return exc
    finally:
        await queue.put(_Q_EXEC_DONE)


def _quota_to_frame(blocked: QueryQuotaBlockedError) -> dict:
    if blocked.quota_payload:
        return {
            "code": blocked.error_code,
            "message": blocked.message,
            "quota": blocked.quota_payload,
        }
    return {
        "code": blocked.error_code,
        "message": blocked.message,
    }


def _to_response(outcome: GlobalAssistantOutcome) -> AssistantQueryResponse:
    return AssistantQueryResponse(
        answer=outcome.answer,
        resolution_status=outcome.resolution_status,
        resolution_source=outcome.resolution_source,
        candidates=list(outcome.candidates),
        project_key=outcome.project_key,
        meta=outcome.meta,
        rag_ran=outcome.rag_ran,
        replayed=outcome.replayed,
        trace_id=outcome.trace_id,
        latency_ms=outcome.latency_ms,
    )
