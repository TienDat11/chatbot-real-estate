"""FastAPI app factory — thin routers, no business logic.

Routes:
  POST /query           — SSE when `Accept: text/event-stream`, else JSON
  POST /llms-hello      — first-open LLM greeting (router: api.interfaces.api.hello)
  GET  /api/anon/token  — signed anonymous identity mint (router: api.interfaces.api.anon_routes)
  GET  /health          — liveness
  GET  /ready           — PG reachable + LightRAG init flag
  GET  /sources/{doc_id}— registry metadata + validity status

Lifespan: LightRAG init stays lazy by default but is prewarmed in a startup
background task when RAG_PREWARM_ENABLED (default ON) — failure degrades to
the lazy path, never blocking startup. The lead-mirror reconciliation sweep
starts only under FIREBASE_BINDING=firestore.
SSE event order: routing -> places -> sources -> facts -> token -> done (error before done on failure).
"""  # noqa: E501

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api import get_cfg
from api.domain.value_objects.constants import (
    MAX_QUERY_LENGTH,
    SSE_STREAM_TIMEOUT_CODE,
    SSE_STREAM_TIMEOUT_MESSAGE,
    SSE_TOTAL_TIMEOUT_S,
)
from api.interfaces.api.deps import AuthenticatedPrincipal

logger = logging.getLogger("api.main")

# FE replays full assistant answers in `history`; this cap is DoS-only, the
# pipeline truncates each turn to MAX_QUERY_LENGTH before any LLM call.
MAX_HISTORY_CONTENT_LENGTH = 8000


# Typed request/response models.
class HistoryTurn(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., min_length=1, max_length=MAX_HISTORY_CONTENT_LENGTH)


class TrainingQueryContext(BaseModel):
    """Training context envelope (G3-r6 spec §10.2, FR-25 revised).

    Training questions legitimately name a real project; the context carries
    exactly that project key because training rides the SHARED project corpus
    (retrieval is scoped to this key, never to a training pseudo-namespace).
    Unknown keys are rejected: a client can
    never smuggle extra pipeline knobs inside the context.
    """

    model_config = ConfigDict(extra="forbid")

    project_key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    # Story 10.1: the question is scoped to one project. The FE contract marks
    # the field required, but the BE schema stays lenient so legacy clients
    # that omit it still reach the default-rule in resolve_project_key (exactly
    # one active project -> that project; >1 active -> 422 PROJECT_SCOPE).
    project_key: str | None = None
    # Training is an explicit contract; Pydantic rejects unknown modes with 422.
    answer_mode: Literal["normal", "training"] | None = None
    session_id: str | None = None
    # D7: anonymous persistent device id (UUID v4) — stable context prefix.
    device_id: str | None = Field(default=None, max_length=64)
    # Secure wave G2: server-minted signed identity carrying the quota state.
    # Missing/tampered values self-heal into a fresh mint echoed back in the
    # response; the length bound is DoS-only (real tokens are ~100 chars).
    anon_token: str | None = Field(default=None, max_length=512)
    as_of: str | None = None
    history: list[HistoryTurn] | None = None
    # G3-r6: present if and only if answer_mode == "training" (both
    # directions are 422), so normal traffic stays byte-identical while the
    # training surface gains its project-context metadata contract.
    context: TrainingQueryContext | None = None

    @model_validator(mode="after")
    def _context_matches_answer_mode(self) -> QueryRequest:
        if self.answer_mode == "training" and self.context is None:
            raise ValueError("training requires context.project_key")
        if self.answer_mode != "training" and self.context is not None:
            raise ValueError("context is only valid in training mode")
        return self


class SourceItem(BaseModel):
    doc_id: str
    title: str
    section: str | None = None
    effective_from: str | None = None
    kind: str | None = None


class FactItem(BaseModel):
    fe_id: str
    subject: str | None = None
    policy_key: str | None = None
    fields: dict = Field(default_factory=dict)
    note: str | None = None


class PlaceItem(BaseModel):
    name: str
    kinds: list[str] = Field(default_factory=list)
    lat: float
    lng: float
    distance_m: float | None = None
    address: str | None = None
    rating: float | None = None


class ProjectRedirect(BaseModel):
    """Structured switch signal for a cross-project question (guardrail).

    Present only when the conv guard detected another KNOWN project in the
    question: the FE renders a project-switch button from these two fields.
    """

    project_key: str
    display_name: str


class QuotaUsage(BaseModel):
    """Per-response allowance view (spec §5.1/§5.4). Nulls mean unlimited or
    untracked: authenticated principals carry cap=null, degraded enforcement
    carries the all-null anonymous shape."""

    used_turns: int | None = None
    remaining_turns: int | None = None
    cap: int | None = None
    is_authenticated: bool = False
    bonus_granted: int | None = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    facts: list[FactItem]
    images: list[dict] = Field(default_factory=list)
    videos: list[dict] = Field(default_factory=list)
    places: list[PlaceItem] = Field(default_factory=list)
    confidence: str
    requires_review: bool
    routing: dict
    trace_id: str
    latency_ms: int
    conv_state: str | None = None
    conversation_directive: str | None = None
    slots_prefill: str | None = None
    project_redirect: ProjectRedirect | None = None
    quota: QuotaUsage | None = None
    # Present only when the caller had no usable token and one was minted;
    # FE must persist it (spec §5.1 self-heal path).
    anon_token: str | None = None
    lead_cta_hint: str | None = None
    # Explicit terminal status lets JSON and SSE consumers distinguish a
    # completed answer from a transport-level response with no payload.
    status: str = "completed"
    history_persisted: bool = False
    # G3-r6 §10.2: present on training answers only (None elsewhere). The
    # fields are additive; every pre-existing key keeps its contract.
    training_session_id: str | None = None
    context: TrainingQueryContext | None = None


# SSE helpers.
_SSE_RESPONSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _normalize_history(history: list[HistoryTurn] | None) -> list[dict[str, str]]:
    """Prepare replayed turns for the pipeline.

    FE replays full assistant answers, so each turn is truncated to
    MAX_QUERY_LENGTH and whitespace-only turns are dropped before any
    pipeline stage (rewrite/guard/answer) sees them. Role validity is
    already enforced by the HistoryTurn schema.
    """
    return [
        {"role": t.role, "content": t.content[:MAX_QUERY_LENGTH]}
        for t in (history or [])
        if t.content.strip()
    ]


class TrainingPreflightConflictError(HTTPException):
    """Training preflight conflict carrying a stable machine-readable code.

    Subclasses HTTPException so the existing ``except HTTPException: raise``
    guard in the preflight re-raises it untouched (never swallowed by the
    broad store-failure handler), while the /query handler catches it by type
    and renders the structured ``{"ok": False, "error": {code, message}}``
    envelope the FE branches on — a self-healable session conflict versus a
    non-retryable inactive project. The inherited ``detail`` keeps a sane
    default body should the exception ever escape the handler.
    """

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(status_code=409, detail=message)
        self.code = code


async def _validate_training_request(
    req: QueryRequest,
    principal: AuthenticatedPrincipal | None,
) -> str:
    """G3-r6 §10.2 preflight for a training turn; returns the context key.

    Runs BEFORE the quota reservation and before any pipeline leg, so an
    invalid training request costs nothing and cannot touch retrieval:
    422 shape/context errors, 403 cross-owner session access, 409 inactive
    context project (or a session already bound to a different context).
    The bind is idempotent: a repeat of the same (owner, session, project)
    converges without touching messages.
    """
    from api.application.services.chat_history_service import (  # noqa: PLC0415
        ChatHistoryService,
    )
    from api.application.services.project_config import (  # noqa: PLC0415
        load_project_registry_record,
    )
    from api.application.training_history import (  # noqa: PLC0415
        TrainingContextRequiredError,
        TrainingProjectInactiveError,
        TrainingProjectUnknownError,
        require_training_context_key,
        validate_training_project,
    )
    from api.infrastructure.adapters.postgres_chat_history import (  # noqa: PLC0415
        repository as chat_history_repository,
    )

    if principal is None or req.context is None:
        # Defense in depth: the auth dependency + schema should have rejected
        # this already; a None here means the call site mis-wired training.
        raise HTTPException(status_code=422, detail="training requires a verified sales principal")
    try:
        context_key = require_training_context_key(req.context.project_key)
    except TrainingContextRequiredError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = await load_project_registry_record(context_key)
    try:
        validate_training_project(
            context_key, status=(record.status if record is not None else None)
        )
    except TrainingProjectInactiveError as exc:
        raise TrainingPreflightConflictError(
            code="TRAINING_PROJECT_INACTIVE", message=str(exc)
        ) from exc
    except TrainingProjectUnknownError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not req.session_id:
        raise HTTPException(
            status_code=422, detail="training requires session_id for durable history"
        )
    history = ChatHistoryService(chat_history_repository)
    try:
        existing = await history.training_session(session_id=req.session_id)
        if existing is not None:
            if existing.owner_firebase_uid != principal.firebase_uid:
                raise HTTPException(
                    status_code=403, detail="Training session belongs to another account"
                )
            if existing.context_project_key != context_key:
                raise TrainingPreflightConflictError(
                    code="TRAINING_SESSION_CONTEXT_MISMATCH",
                    message="Training session is bound to a different project context",
                )
            return context_key
        bound = await history.bind_training_session(
            session_id=req.session_id,
            owner_firebase_uid=principal.firebase_uid,
            context_project_key=context_key,
        )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — history store failure must not leak internals
        logger.warning("training session preflight failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Training history store unavailable") from None
    if not bound:
        # The bind predicate refuses to touch any row that is not an
        # unowned training session — typically the id already carries a
        # CUSTOMER conversation, which training must never hijack.
        raise TrainingPreflightConflictError(
            code="TRAINING_SESSION_CONFLICT",
            message="session_id is already in use for a different conversation",
        )
    return context_key


def _pipe_run_accepts_training_context(pipe) -> bool:
    """True when pipe.run's signature accepts `training_context_project_key`.

    Decided purely by signature inspection, never by catching a call-time
    TypeError: a TypeError raised inside a custom pipeline body is a pipeline
    bug and must surface as such, not be mistaken for a signature mismatch.
    Legacy/custom pipelines without the keyword (or unknown signatures) simply
    omit it.
    """
    try:
        params = inspect.signature(pipe.run).parameters
    except (TypeError, ValueError):  # exotic callable: omit rather than break
        return False
    if "training_context_project_key" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _sse_stream(
    pipe,
    req: QueryRequest,
    as_of: str | None,
    project_key: str,
    device_id: str | None,
    quota_gate,
    turn_context,
    bearer_id_token: str | None = None,
    request: Request | None = None,
    total_timeout_s: float | None = None,
    training_owner_uid: str | None = None,
    training_context_key: str | None = None,
) -> AsyncIterator[str]:
    """Run the pipeline emitting SSE; always emits `done` (even after errors).

    Total-deadline contract: the whole stream must terminate within
    `total_timeout_s` (default: settings.sse_total_timeout_s) of the stream
    start, no matter how the pipeline is stuck. Heartbeats keep proxies alive
    but NEVER reset the deadline — it is a wall clock anchored at `started_at`.
    When the deadline elapses the pipeline task is cancelled and a terminal
    `error` (code STREAM_TIMEOUT + retry copy) then `done` frame is emitted so
    the FE re-enables the composer and keeps the partial answer. The finalize
    await is also bounded, so a task that ignores cancellation cannot park the
    generator (and with it the client) forever.

    Quota contract (spec §5.3): the turn is reserved ATOMICALLY before this
    stream starts (prepare_turn), so `ack` carries the post-reservation
    snapshot (plus a freshly minted token); a failed pipeline REFUNDS the
    reservation and emits `error` then `done` — no token frames leak.
    """
    from api.application.pipelines.workflow import QueryRejected  # noqa: PLC0415
    from api.application.services.query_quota_gate import (  # noqa: PLC0415
        AnonymousTurnContext as GateAnonymousTurnContext,
    )

    if total_timeout_s is None:
        total_timeout_s = float(get_cfg("sse_total_timeout_s", SSE_TOTAL_TIMEOUT_S))
    if total_timeout_s <= 0:
        total_timeout_s = SSE_TOTAL_TIMEOUT_S

    q: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    async def on_event(event: str, data: dict) -> None:
        if req.answer_mode == "training" and event == "routing":
            data = {key: value for key, value in data.items() if key != "lead_cta_hint"}
        await q.put((event, data))

    history = _normalize_history(req.history)

    # Settlement guard for the reserved turn: flips to True exactly once, by
    # either the refund path (failed or aborted pipeline) or the finalize path
    # (successful answer). That makes every refund idempotent — a disconnect or
    # deadline cut racing an already-refunded crash can never refund twice, and
    # a refund never follows a finalized success.
    turn_released = False

    async def _refund_reserved_turn() -> None:
        # A reserved turn is released only when the pipeline failed (spec §4 R1);
        # authenticated/unmanaged contexts carry no reservation to refund.
        nonlocal turn_released
        if (
            not turn_released
            and hasattr(turn_context, "identity_key")
            and hasattr(turn_context, "project_key")
        ):
            await quota_gate.refund_turn(turn_context)
            turn_released = True

    async def run_pipe() -> None:
        nonlocal turn_released
        try:
            # Modern pipelines get the optional training context layer;
            # legacy/custom run() signatures keep working without it.
            run_kwargs: dict[str, object] = {
                "project_key": project_key,
                "device_id": device_id,
                "on_event": on_event,
            }
            if _pipe_run_accepts_training_context(pipe):
                run_kwargs["training_context_project_key"] = training_context_key
            payload = await pipe.run(
                req.query,
                req.session_id,
                as_of,
                history,
                **run_kwargs,
            )
            if req.answer_mode == "training":
                # Training answers are isolated from customer conversion state.
                payload = {
                    key: value for key, value in payload.items()
                    if key not in {"lead_cta_hint", "project_redirect"}
                }
                payload["lead_cta_hint"] = None
                payload["project_redirect"] = None
                # G3-r6 §10.2: terminal payload mirrors the ack binding.
                payload["training_session_id"] = req.session_id
                payload["context"] = {"project_key": training_context_key}
            # Keep the terminal payload authoritative for every successful
            # transport. SSE done and JSON must expose the same semantic data.
            payload["status"] = "completed"
            # Pipelines may return None when history persistence is unavailable;
            # normalize it to the explicit false contract without changing false.
            payload["history_persisted"] = bool(payload.get("history_persisted"))
            payload.setdefault("videos", [])
            payload["quota"] = turn_context.quota_payload()
            if (
                hasattr(turn_context, "anon_token_is_newly_minted")
                and turn_context.anon_token_is_newly_minted
            ):
                payload["anon_token"] = turn_context.anon_token
            from api.application.services.chat_history_service import (
                ChatHistoryService,  # noqa: PLC0415
            )
            from api.infrastructure.adapters.postgres_chat_history import (
                repository as chat_history_repository,  # noqa: PLC0415
            )

            try:
                if req.answer_mode == "training":
                    # G3-r6: training turns persist on the PRIVATE staff
                    # surface (owner + context_project_key), never through the
                    # customer append path — a training row can never hydrate
                    # a customer session view and vice versa.
                    payload["history_persisted"] = bool(
                        await ChatHistoryService(chat_history_repository).persist_training_turn(
                            session_id=req.session_id,
                            owner_firebase_uid=training_owner_uid or "",
                            context_project_key=training_context_key or "",
                            user_content=req.query,
                            assistant_content=str(payload.get("answer") or ""),
                            assistant_meta={
                                key: payload.get(key)
                                for key in ("sources", "facts", "images")
                                if payload.get(key) is not None
                            },
                        )
                    )
                else:
                    payload["history_persisted"] = bool(
                        await ChatHistoryService(chat_history_repository).persist_turn(
                        session_id=req.session_id,
                        device_id=device_id,
                        project_key=project_key,
                        identity_key=getattr(turn_context, "identity_key", None),
                        user_content=req.query,
                        assistant_content=str(payload.get("answer") or ""),
                        assistant_meta={
                            key: payload.get(key)
                            for key in (
                                "sources",
                                "facts",
                                "images",
                                "project_redirect",
                                "lead_cta_hint",
                            )
                            if payload.get(key) is not None
                        },
                        )
                    )
            except Exception:  # noqa: BLE001 — history is a non-critical side effect
                payload["history_persisted"] = False
                logger.warning(
                    "chat history persistence failed after successful answer", exc_info=True
                )
            payload["lead_cta_hint"] = (
                None
                if req.answer_mode == "training"
                else await _durable_lead_cta_hint(
                    req.session_id,
                device_id=device_id,
                project_key=project_key,
                identity_key=getattr(turn_context, "identity_key", None),
                authenticated=bool(
                    bearer_id_token
                    and getattr(turn_context, "quota_payload", lambda: {})().get("is_authenticated")
                ),
                )
            )
            finalize = getattr(quota_gate, "finalize_turn", None)
            if (
                callable(finalize)
                and hasattr(turn_context, "identity_key")
                and hasattr(turn_context, "project_key")
                and not turn_released
            ):
                await finalize(turn_context)
                # The answer is finalized: the reservation is settled and must
                # never be refunded from here on (the client keeps the turn).
                turn_released = True
            await q.put(("__done__", payload))
        except QueryRejected as exc:
            await _refund_reserved_turn()
            await q.put(("__rejected__", {"message": exc.reason}))
        except Exception:  # noqa: BLE001 — always emit error + done
            logger.exception("sse pipeline crashed")
            await _refund_reserved_turn()
            # No str(exc) here: exception text is logged server-side only, the
            # client receives a stable INTERNAL code (never internal details).
            await q.put(("__crashed__", {}))

    # Emit ack immediately so FE shows zero-latency feedback (< 100ms). The
    # atomic reservation already happened in prepare_turn, so this snapshot is
    # the post-reservation view.
    ack_data: dict = {
        "received": True,
        "ts": int(asyncio.get_event_loop().time() * 1000),
        "quota": turn_context.quota_payload(),
    }
    if (
        isinstance(turn_context, GateAnonymousTurnContext)
        and turn_context.anon_token_is_newly_minted
    ):
        ack_data["anon_token"] = turn_context.anon_token
    if req.answer_mode == "training":
        # G3-r6 §10.2: the ack echoes the private session binding so the
        # training FE can attach follow-up turns to the same durable session.
        ack_data["training_session_id"] = req.session_id
        ack_data["context"] = {"project_key": training_context_key}
    logger.info(
        "sse first-answer ack project=%s training=%s",
        project_key,
        req.answer_mode == "training",
    )
    yield _frame("ack", ack_data)
    started_at = asyncio.get_event_loop().time()
    deadline = started_at + total_timeout_s
    first_event_at: float | None = None
    task = asyncio.create_task(run_pipe())
    try:
        while True:
            if request is not None and await request.is_disconnected():
                logger.info(
                    "sse disconnected project=%s elapsed_ms=%d",
                    project_key,
                    int((asyncio.get_event_loop().time() - started_at) * 1000),
                )
                task.cancel()
                break
            # Total-deadline check (wall clock): heartbeats below never reset
            # this deadline, so a pipeline that stalls end-to-end still ships a
            # terminal error+done frame instead of keeping the stream alive via
            # ": heartbeat" forever.
            now = asyncio.get_event_loop().time()
            remaining = deadline - now
            if remaining <= 0:
                logger.warning(
                    "sse total deadline exceeded project=%s elapsed_ms=%d",
                    project_key,
                    int((now - started_at) * 1000),
                )
                task.cancel()
                # The pipeline may have completed concurrently at the wire: if a
                # terminal event is already queued, honor it instead of the
                # timeout frame (never error after the work actually finished).
                try:
                    event, data = q.get_nowait()
                except asyncio.QueueEmpty:
                    event = "__timeout__"
                    data = {}
                if event in ("__done__", "__rejected__", "__crashed__"):
                    if event == "__rejected__":
                        yield _frame("error", {"code": "REJECTED", "message": data["message"]})
                        yield _frame("done", {})
                    elif event == "__crashed__":
                        yield _frame("error", {"code": "INTERNAL", "message": "internal error"})
                        yield _frame("done", {})
                    else:
                        yield _frame("done", data)
                else:
                    # Terminal timeout frame with user-visible retry text; the
                    # FE keeps the partial answer and offers retry (same path as
                    # a mid-stream network cut).
                    logger.info(
                        "sse timeout frame project=%s elapsed_ms=%d",
                        project_key,
                        int((now - started_at) * 1000),
                    )
                    yield _frame(
                        "error",
                        {"code": SSE_STREAM_TIMEOUT_CODE, "message": SSE_STREAM_TIMEOUT_MESSAGE},
                    )
                    yield _frame("done", {})
                break
            try:
                event, data = await asyncio.wait_for(
                    q.get(), timeout=min(15.0, remaining)
                )
            except asyncio.TimeoutError:
                # SSE comment frames keep proxies alive and are ignored by
                # parsers. Emitted ONLY while time remains on the total
                # deadline, so the heartbeat can never extend the stream.
                if asyncio.get_event_loop().time() < deadline:
                    yield ": heartbeat\n\n"
                continue
            if first_event_at is None:
                first_event_at = asyncio.get_event_loop().time()
                logger.info(
                    "sse first-event project=%s elapsed_ms=%d",
                    project_key,
                    int((first_event_at - started_at) * 1000),
                )
            if event == "images":
                logger.info(
                    "sse media project=%s count=%d",
                    project_key,
                    len(data.get("images", [])),
                )
            if event == "__done__":
                logger.info(
                    "sse done project=%s elapsed_ms=%d truncated=%s",
                    project_key,
                    int((asyncio.get_event_loop().time() - started_at) * 1000),
                    bool(data.get("answer_truncated")),
                )
            if event in ("__done__", "__rejected__", "__crashed__"):
                if event == "__rejected__":
                    yield _frame("error", {"code": "REJECTED", "message": data["message"]})
                    yield _frame("done", {})
                elif event == "__crashed__":
                    # Stable, non-leaky error code (mirrors the JSON 500 path).
                    yield _frame("error", {"code": "INTERNAL", "message": "internal error"})
                    yield _frame("done", {})
                else:
                    yield _frame("done", data)
                break
            yield _frame(event, data)
    finally:
        try:
            if request is not None and await request.is_disconnected():
                task.cancel()
            # Bounded finalize: a task that ignores cancellation must not park the
            # generator (and the client) forever — settle within a short window.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001
                logger.exception("sse task finalize")
        finally:
            # Every exit path that reached here without the pipeline settling
            # the reservation — client disconnect, total-deadline cut, generator
            # close, or a cancelled pipeline with no terminal event — refunds
            # the reserved turn (spec §4 R1). Idempotent via turn_released:
            # run_pipe already refunded (rejected/crashed/empty-corpus) or
            # finalized (successful answer) is never touched, and staff/unmanaged
            # contexts carry no reservation to refund.
            try:
                await _refund_reserved_turn()
            except Exception:  # noqa: BLE001 — never mask the stream teardown
                logger.warning("sse turn refund failed during teardown", exc_info=True)


async def _sse_blocked_stream(blocked) -> AsyncIterator[str]:
    """Pre-pipeline refusal in SSE clothing.

    SSE is HTTP 200 by nature (spec §5.3): the structured `error` frame
    replaces the answer and `done` closes the stream; no token frames exist
    because the pipeline never started.
    """
    yield _frame("error", blocked.error_frame_data())
    yield _frame("done", {})


# App factory.
async def _durable_lead_cta_hint(
    session_id: str | None,
    *,
    device_id: str | None,
    project_key: str,
    identity_key: str | None,
    authenticated: bool,
) -> str | None:
    """Derive CTA state from an already-authorized session context.

    Never query CTA state by session id alone: session ids are opaque routing
    values, not credentials. Authenticated callers retain the existing CTA
    suppression, while anonymous callers must carry the verified signed
    identity used to authorize the session.
    """
    if not session_id or authenticated or not device_id or not identity_key:
        return None
    from api.infrastructure.adapters.postgres_chat_history import repository  # noqa: PLC0415

    try:
        turns, handed_off, phone_given = await repository.get_cta_state(
            session_id=session_id,
            device_id=device_id,
            project_key=project_key,
            identity_key=identity_key,
        )
    except Exception:
        return None
    if handed_off or phone_given or turns < int(get_cfg("lead_cta_after_turns", 3)):
        return None
    return (
        "Anh/chị để lại số điện thoại nhé, chuyên viên gọi lại trong ~5 phút để tư vấn căn phù hợp."
    )


async def _chat_history_retention_loop() -> None:
    from api.infrastructure.adapters.postgres_chat_history import repository

    while True:
        try:
            await repository.cleanup(retention_days=int(get_cfg("chat_history_retention_days", 30)))
        except Exception:
            logger.warning("chat history retention cleanup failed", exc_info=True)
        await asyncio.sleep(86400)


async def _wire_postgres_rate_limit_port() -> None:
    """Install the durable cross-worker IP brake when Postgres is reachable.

    Startup NEVER crashes on this: the process-local InMemory fallback stays
    installed when PG is down (or comes up later), matching the rule that a
    degraded secondary brake must not take the product offline. The pool is
    the shared lead RW pool, closed by _close_persistence_pools on shutdown.
    """
    from api.application.services.anon_identity import set_rate_limit_port  # noqa: PLC0415
    from api.infrastructure.adapters.postgres_leads import get_lead_pool  # noqa: PLC0415
    from api.infrastructure.adapters.postgres_quota import PostgresQuotaStore  # noqa: PLC0415

    try:
        await get_lead_pool()
        set_rate_limit_port(PostgresQuotaStore())
        logger.info("anonymous IP rate-limit port wired to postgres")
    except Exception:  # noqa: BLE001 — availability over enforcement here
        logger.warning(
            "postgres unavailable at startup; anonymous IP brake stays in-process memory",
            exc_info=True,
        )


def _maybe_start_lead_mirror_reconciliation() -> asyncio.Task | None:
    """Start the mirror reconciliation sweep only when it can do work.

    The guard lives here (not inside the loop) so the default off-binding
    deployment pays zero cost — not even an import of the reconciliation
    module or a created task.
    """
    binding = str(get_cfg("firebase_binding", "") or "").strip().lower()
    if binding != "firestore":
        return None
    if not bool(get_cfg("lead_mirror_reconciliation_enabled", True)):
        return None
    from api.application.services.lead_mirror_reconciliation import (  # noqa: PLC0415
        run_lead_mirror_reconciliation_loop,
    )

    return asyncio.create_task(run_lead_mirror_reconciliation_loop())


# Budget for one prewarm attempt: cold LightRAG init measures ~30-40s, so a 90s
# bound absorbs slow PG/first-embedding variance while still failing a hung init
# (a stuck attempt would otherwise never retry or give up).
RAG_PREWARM_TIMEOUT_S = 90.0


async def _prewarm_rag_pipeline() -> None:
    """Warm the lazy RAG pipeline in the background at startup.

    Reuses the exact first-query init path (rag_leg._get_rag: LightRAG
    singleton + one-shot initialize_storages), so the work that otherwise
    blocks the first Soleil query (~30-40s) happens while the worker boots.
    Idempotent by construction: get_lightrag is a module singleton and the
    storage DDL runs once per process. Startup NEVER crashes on this — a
    failure is logged, retried ONCE, then given up with the lazy path intact.
    """
    from api.application.services.rag_leg import _get_rag  # noqa: PLC0415

    for attempt in (1, 2):
        try:
            await asyncio.wait_for(_get_rag(), timeout=RAG_PREWARM_TIMEOUT_S)
            logger.info("rag pipeline prewarm completed (attempt %d)", attempt)
            return
        except Exception:  # noqa: BLE001 — prewarm is best-effort, never fatal
            logger.warning(
                "rag pipeline prewarm failed (attempt %d/2); falling back to lazy first-query init",
                attempt,
                exc_info=True,
            )
    logger.error("rag pipeline prewarm gave up after 2 attempts; first query cold-starts")


def _maybe_start_rag_prewarm() -> asyncio.Task | None:
    """Start the RAG prewarm only when the operator enabled it.

    The flag lives here (not inside the task) so a disabled deployment pays
    zero cost — not even an import of the LightRAG machinery. Idempotency is
    guaranteed inside _prewarm_rag_pipeline / rag_leg, so a re-startup in the
    same process is a no-op.
    """
    if not bool(get_cfg("rag_prewarm_enabled", True)):
        return None
    return asyncio.create_task(_prewarm_rag_pipeline())


async def _close_persistence_pools() -> None:
    """Close every module-level asyncpg pool; shutdown-only, best-effort."""
    from api.application.services.audit import close_audit_pool  # noqa: PLC0415
    from api.application.services.sql_leg import close_ro_pool  # noqa: PLC0415
    from api.domain.services.nl2sql_guard import close_nl2sql_pool  # noqa: PLC0415
    from api.infrastructure.adapters.firestore_rest_mirror import (  # noqa: PLC0415
        close_client as close_firestore_mirror_client,
    )
    from api.infrastructure.adapters.postgres_leads import close_lead_pool  # noqa: PLC0415
    from api.infrastructure.dependencies import get_project_registry  # noqa: PLC0415

    async def close_project_registry_pool() -> None:
        registry = get_project_registry()
        close = getattr(registry, "close", None)
        if close is not None:
            await close()

    for closer in (
        close_ro_pool,
        close_nl2sql_pool,
        close_audit_pool,
        close_lead_pool,
        close_project_registry_pool,
        close_firestore_mirror_client,
    ):
        try:
            await closer()
        except Exception:  # noqa: BLE001
            logger.warning("pool close fail", exc_info=True)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: conditionally launch the RAG prewarm and the lead-mirror
    reconciliation sweep (both background tasks, never blocking startup).

    Shutdown: cancel both tasks (the prewarm only ever awaits the idempotent
    LightRAG init; the sweep only sleeps between batches, so cancel is safe
    for both) and close every persistence pool.
    """
    await _wire_postgres_rate_limit_port()
    prewarm_task = _maybe_start_rag_prewarm()
    reconciliation_task = _maybe_start_lead_mirror_reconciliation()
    retention_task = asyncio.create_task(_chat_history_retention_loop())
    yield
    if prewarm_task is not None:
        prewarm_task.cancel()
        with suppress(asyncio.CancelledError):
            await prewarm_task
    retention_task.cancel()
    with suppress(asyncio.CancelledError):
        await retention_task
    if reconciliation_task is not None:
        reconciliation_task.cancel()
        with suppress(asyncio.CancelledError):
            await reconciliation_task
    await _close_persistence_pools()


def create_app() -> FastAPI:
    from api.infrastructure.config.config import export_runtime_env  # noqa: PLC0415

    export_runtime_env()
    app = FastAPI(
        title="rag-real-estate",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    # CORS allowlist from Settings. Config validation (config.py
    # _enforce_cors_origins_safety) fails closed: production without an
    # explicit CORS_ORIGINS refuses to start, and the "*" wildcard is rejected
    # in every environment because the API carries credentialed/auth endpoints.
    # Methods/headers stay permissive because the origin allowlist above is the
    # actual CORS boundary.
    origins = get_cfg("cors_origins")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if isinstance(origins, list) else [origins],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from api.interfaces.api.admin_routes import (  # noqa: PLC0415
        admin_session_router,
        sales_session_router,
    )
    from api.interfaces.api.anon_routes import router as anon_router  # noqa: PLC0415
    from api.interfaces.api.auth_routes import router as auth_router  # noqa: PLC0415
    from api.interfaces.api.crm_routes import router as crm_router  # noqa: PLC0415
    from api.interfaces.api.deps import require_training_sales  # noqa: PLC0415
    from api.interfaces.api.hello import router as hello_router  # noqa: PLC0415
    from api.interfaces.api.lead import router as lead_router  # noqa: PLC0415
    from api.interfaces.api.notifications import router as notifications_router  # noqa: PLC0415
    from api.interfaces.api.projects import router as projects_router  # noqa: PLC0415
    from api.interfaces.api.sales import router as sales_router  # noqa: PLC0415
    from api.interfaces.api.sessions import router as sessions_router  # noqa: PLC0415

    app.include_router(lead_router)
    app.include_router(sales_router)
    app.include_router(hello_router)
    app.include_router(projects_router)
    app.include_router(admin_session_router)
    app.include_router(sales_session_router)
    app.include_router(crm_router)
    app.include_router(sessions_router)
    app.include_router(anon_router)
    app.include_router(auth_router)
    app.include_router(notifications_router)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "app": "rag-real-estate", "version": "0.1.0"}

    @app.get("/ready")
    async def ready() -> dict:
        """PG reachable + LightRAG init flag (set by rag_leg on successful get_lightrag)."""
        from api.application.services.rag_leg import LIGHTRAG_READY  # noqa: PLC0415
        from api.application.services.sql_leg import get_ro_pool  # noqa: PLC0415

        checks: dict = {"pg": False, "lightrag": LIGHTRAG_READY}
        try:
            pool = await get_ro_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["pg"] = True
        except Exception:  # noqa: BLE001
            logger.warning("readiness postgres check failed", exc_info=True)
            checks["pg_status"] = "unavailable"
        checks["ok"] = bool(checks["pg"] and checks["lightrag"])
        return checks

    @app.get("/sources/{doc_id}")
    async def source_info(doc_id: str) -> dict:
        """Registry metadata + validity status for one doc."""
        from api.application.services.sql_leg import get_ro_pool  # noqa: PLC0415

        pool = await get_ro_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT doc_id, kind, title, source_file, effective_from, effective_to, status, version, metadata "  # noqa: E501
                "FROM documents WHERE doc_id = $1",
                doc_id,
            )
        if not row:
            raise HTTPException(status_code=404, detail=f"doc {doc_id} không tồn tại")
        data = dict(row)
        data["effective_from"] = (
            data["effective_from"].isoformat() if data.get("effective_from") else None
        )
        data["effective_to"] = (
            data["effective_to"].isoformat() if data.get("effective_to") else None
        )
        return data

    @app.post("/query", response_model=QueryResponse)
    async def query(
        req: QueryRequest,
        request: Request,
        # Reviewer blocker 1: server-side verified sales authorization for
        # training mode. The dependency reads the JSON body to detect
        # answer_mode="training" (Starlette caches it, so req stays intact)
        # and enforces a verified, active, mapped sales principal; normal-mode
        # requests pass through untouched. It must run BEFORE any pipeline or
        # quota reservation so unauthorized training calls cost nothing.
        _training_auth: AuthenticatedPrincipal | None = Depends(  # noqa: B008
            require_training_sales
        ),
    ) -> StreamingResponse | dict:
        from api.application.pipelines.conv_workflow import RagQueryPipelineConv  # noqa: PLC0415
        from api.application.pipelines.workflow import QueryRejected  # noqa: PLC0415
        from api.application.services.project_scope import (  # noqa: PLC0415
            ProjectScopeError,
            resolve_project_key,
        )
        from api.interfaces.api.anon_routes import get_client_ip_address  # noqa: PLC0415

        # G3-r6 §10.2: the training preflight (context shape, active project,
        # owner-bound durable session) runs before quota and before any leg,
        # so an invalid training turn costs nothing. Normal mode is untouched.
        training_context_key: str | None = None
        if req.answer_mode == "training":
            try:
                training_context_key = await _validate_training_request(req, _training_auth)
            except TrainingPreflightConflictError as exc:
                # Stable machine-readable code in the same envelope /query
                # already uses for PROJECT_SCOPE/REJECTED, so the FE can tell a
                # self-healable session conflict from a non-retryable inactive
                # project without string-matching the message.
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"ok": False, "error": {"code": exc.code, "message": exc.detail}},
                )

        # Story 10.1 [RV-22/08]: resolve the active project BEFORE any leg runs —
        # >1 active project with no explicit choice is a 422 that prompts the
        # ProjectPicker; exactly one active project is the safe default.
        try:
            project_key = (
                # Mode marker only: the pipeline routes retrieval to the real
                # context project (shared corpus); '_training' never scopes data.
                "_training" if req.answer_mode == "training"
                else await resolve_project_key(req.project_key)
            )
        except ProjectScopeError as exc:
            # Story 10.3: carry the pickable project list in the 422 body so the
            # FE popup renders immediately without a second round-trip to
            # GET /api/projects. The catalogue read is offloaded off the event
            # loop (M12): best-effort like the endpoint — a dead DB yields
            # projects: [] and the FE falls back to its static catalogue.
            from api.application.services.project_config import (
                load_project_catalogue,  # noqa: PLC0415
            )

            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error": {"code": "PROJECT_SCOPE", "message": str(exc)},
                    "projects": await load_project_catalogue(),
                },
            )

        # Secure wave Issue 2: server-enforced quota gate (spec §4/§5). Runs
        # AFTER project scope (422 contract above stays byte-unchanged) and
        # BEFORE any pipeline leg, so refused turns cost nothing. The gate
        # atomically RESERVES the anonymous turn here; a failed pipeline refunds
        # it (spec §4 R1), and a store failure is fail-closed (503).
        from api.application.services.query_quota_gate import (  # noqa: PLC0415
            QueryQuotaBlockedError,
            build_query_quota_gate,
            build_quota_blocked_json_body,
        )

        quota_gate = build_query_quota_gate()
        authorization_header = request.headers.get("authorization") or ""
        bearer_id_token = (
            authorization_header[7:].strip()
            if authorization_header.lower().startswith("bearer ")
            else None
        )
        try:
            turn_context = await quota_gate.prepare_turn(
                presented_anon_token=req.anon_token,
                bearer_id_token=bearer_id_token,
                client_ip_address=get_client_ip_address(request),
                project_key=project_key,
            )
        except QueryQuotaBlockedError as blocked:
            if "text/event-stream" in (request.headers.get("accept") or ""):
                return StreamingResponse(
                    _sse_blocked_stream(blocked),
                    media_type="text/event-stream",
                    headers=_SSE_RESPONSE_HEADERS,
                )
            return JSONResponse(
                status_code=blocked.status_code,
                content=build_quota_blocked_json_body(blocked),
            )

        accept = request.headers.get("accept") or ""
        if "text/event-stream" in accept:
            pipe = RagQueryPipelineConv()
            return StreamingResponse(
                _sse_stream(
                    pipe,
                    req,
                    req.as_of,
                    project_key,
                    req.device_id,
                    quota_gate,
                    turn_context,
                    bearer_id_token,
                    request,
                    training_owner_uid=(
                        _training_auth.firebase_uid if _training_auth is not None else None
                    ),
                    training_context_key=training_context_key,
                ),
                media_type="text/event-stream",
                headers=_SSE_RESPONSE_HEADERS,
            )
        # JSON mode
        pipe = RagQueryPipelineConv()
        try:
            payload = await pipe.run(
                req.query,
                req.session_id,
                req.as_of,
                _normalize_history(req.history),
                project_key=project_key,
                device_id=req.device_id,
                training_context_project_key=training_context_key,
            )
            if req.answer_mode == "training":
                payload = {
                    key: value for key, value in payload.items()
                    if key not in {"lead_cta_hint", "project_redirect"}
                }
                payload["lead_cta_hint"] = None
                payload["project_redirect"] = None
                # G3-r6 §10.2: JSON transport mirrors the SSE ack binding.
                payload["training_session_id"] = req.session_id
                payload["context"] = {"project_key": training_context_key}
            # Keep the terminal payload authoritative for every successful
            # transport. SSE done and JSON must expose the same semantic data.
            payload["status"] = "completed"
            # Pipelines may return None when history persistence is unavailable;
            # normalize it to the explicit false contract without changing false.
            payload["history_persisted"] = bool(payload.get("history_persisted"))
            payload.setdefault("videos", [])
            payload["quota"] = turn_context.quota_payload()
            if (
                hasattr(turn_context, "anon_token_is_newly_minted")
                and turn_context.anon_token_is_newly_minted
            ):
                payload["anon_token"] = turn_context.anon_token
            from api.application.services.chat_history_service import (
                ChatHistoryService,  # noqa: PLC0415
            )
            from api.infrastructure.adapters.postgres_chat_history import (
                repository as chat_history_repository,  # noqa: PLC0415
            )

            try:
                if req.answer_mode == "training":
                    # G3-r6: private staff training history (owner-scoped),
                    # identical contract to the SSE persist path.
                    payload["history_persisted"] = bool(
                        await ChatHistoryService(chat_history_repository).persist_training_turn(
                            session_id=req.session_id,
                            owner_firebase_uid=(
                                _training_auth.firebase_uid if _training_auth is not None else ""
                            ),
                            context_project_key=training_context_key or "",
                            user_content=req.query,
                            assistant_content=str(payload.get("answer") or ""),
                            assistant_meta={
                                key: payload.get(key)
                                for key in ("sources", "facts", "images")
                                if payload.get(key) is not None
                            },
                        )
                    )
                else:
                    payload["history_persisted"] = bool(
                        await ChatHistoryService(chat_history_repository).persist_turn(
                        session_id=req.session_id,
                        device_id=req.device_id,
                        project_key=project_key,
                        identity_key=getattr(turn_context, "identity_key", None),
                        user_content=req.query,
                        assistant_content=str(payload.get("answer") or ""),
                        assistant_meta={
                            key: payload.get(key)
                            for key in (
                                "sources",
                                "facts",
                                "images",
                                "project_redirect",
                                "lead_cta_hint",
                            )
                            if payload.get(key) is not None
                        },
                        )
                    )
            except Exception:  # noqa: BLE001 — history is a non-critical side effect
                payload["history_persisted"] = False
                logger.warning(
                    "chat history persistence failed after successful answer", exc_info=True
                )
            payload["lead_cta_hint"] = (
                None
                if req.answer_mode == "training"
                else await _durable_lead_cta_hint(
                    req.session_id,
                    device_id=req.device_id,
                    project_key=project_key,
                    identity_key=getattr(turn_context, "identity_key", None),
                    authenticated=bool(
                        bearer_id_token
                        and getattr(
                            turn_context, "quota_payload", lambda: {}
                        )().get("is_authenticated")
                    ),
                )
            )
            finalize = getattr(quota_gate, "finalize_turn", None)
            if (
                callable(finalize)
                and hasattr(turn_context, "identity_key")
                and hasattr(turn_context, "project_key")
            ):
                await finalize(turn_context)
            return payload
        except QueryRejected as exc:
            # The reservation was already taken before the pipeline ran; a
            # rejected pipeline refunds it (spec §4 R1).
            if hasattr(turn_context, "identity_key") and hasattr(turn_context, "project_key"):
                await quota_gate.refund_turn(turn_context)
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": {"code": "REJECTED", "message": exc.reason}},
            )
        except Exception:  # noqa: BLE001 — never leak internal details
            logger.exception("query handler error")
            if hasattr(turn_context, "identity_key") and hasattr(turn_context, "project_key"):
                await quota_gate.refund_turn(turn_context)
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": {"code": "INTERNAL", "message": "internal error"}},
            )

    return app


app = create_app()
