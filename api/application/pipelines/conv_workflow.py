"""Conversation engine shell (Story 4.5, §6.1).
RagRgreConvWorkflow wraps RagQueryWorkflow WITHOUT forking its 8 steps: the
conv layer (ConvContext load -> slot extract -> transition -> routing SSE
event -> CTA hint) runs as a thin LlamaIndex Workflow with 2 steps; the inner
workflow runs to completion as a coroutine from the second step.

The routing SSE event is emitted BEFORE the legs start (§6.6 "emit trước legs")
with intent/conv_state/panel_hint + lead_cta_hint gated by §6.5 (a)-(e).
Cross-project guardrail (project-awareness wave): when the question names a
DIFFERENT known project, start_conv short-circuits after the routing event —
it attaches ``project_redirect`` to routing and returns a deterministic
guidance answer without ever running the inner retrieval workflow.
RagQueryPipeline facade keeps its exact contract so eval/main keep working;
main.py /query is switched to the conv path (back-compat kept).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)

from api.application.pipelines.workflow import (
    EventCallback,
    QueryRejected,
    RagQueryWorkflow,
)
from api.application.services.project_context import ProjectContext
from api.application.services.project_config import brand_token
from api.application.services.conv_state import (
    conv_directive,
    get_context,
    maybe_lead_cta_hint,
    note_useful_turn,
    transition,
)
from api.application.services.output_sanitizer import (
    StreamingSanitizer,
    sanitize_answer_text,
)
from api.domain.services.conv_slots import extract_slots, lead_prefill_note
from api.domain.services.route_intent import Intent, classify_intent
from api.domain.value_objects.constants import SSE_EVENT_TOKEN

logger = logging.getLogger("api.conv_workflow")

SSE_EVENT_ROUTING = "routing"


def _panel_hint(intent: Intent) -> str:
    """Map intent -> FE panel to surface (SSE routing payload)."""
    return {
        Intent.LOCATION: "map",
        Intent.COMPANY: "company",
        Intent.PRICE: "affordability",
        Intent.HANDOFF: "lead",
    }.get(intent, "none")


async def _extract_and_transition(
    query: str,
    session_id: str,
    history: list[dict] | None,
    project_key: str | None,
    device_id: str | None,
) -> dict:
    """Load context, extract slots, transition, return conv metadata dict."""
    ctx = get_context(session_id, device_id)
    if project_key:
        ctx.project_key = project_key  # session nhớ project (story 10.1 AC)
    intent = classify_intent(query, history, project_name=brand_token(project_key)).intent
    res = await extract_slots(query, ctx.slots)
    ctx.slots.update(res["merged"])
    afford_answered = res["deterministic"].get("budget_vnd") is not None or intent == Intent.PRICE
    new_slot = bool(res["deterministic"])
    transition(ctx, intent, new_slot=new_slot, afford_answered=afford_answered)
    return {
        "session_id": session_id,
        "intent": intent.value,
        "conv_state": ctx.state,
        "directive": conv_directive(ctx.state, project_key),
        "slots": dict(ctx.slots),
        "prefill_note": lead_prefill_note(ctx.slots),
        "interested_units": list(ctx.interested_units),
        "project_key": project_key,
    }


class ConvRunEv(Event):
    """Internal: carry conv metadata into the inner-run step."""

    conv: dict
    query: str
    session_id: str | None
    as_of: str | None
    history: list[dict] | None
    project_context: ProjectContext
    device_id: str | None
    visible_limit: int | None = None


class RagRgreConvWorkflow(Workflow):
    """Thin conv shell over RagQueryWorkflow (DO NOT fork the 8 steps)."""

    def __init__(self, timeout: float = 180.0, on_event: EventCallback | None = None):
        super().__init__(timeout=timeout)
        self._streaming_sanitizer = StreamingSanitizer()
        # Assembled sanitized stream text (Bug A): the authoritative record of
        # what actually reached the SSE channel, so an abnormal inner-run exit
        # can still ship a best-effort `done.answer` instead of leaving the FE
        # with unreplaceable partial tokens.
        self._stream_released: list[str] = []
        self.on_event: EventCallback = on_event or (lambda event, data: None)
        # The inner workflow is the only producer of token deltas; handing it the
        # sanitizing wrapper routes every streamed token through the hold-back
        # buffer before it can reach the caller's SSE channel (spec §8).
        self._inner = RagQueryWorkflow(timeout=timeout, on_event=self._emit_sanitized_stream)

    def _emit_sanitized_stream(self, event: str, data: dict) -> Any:
        # Only token deltas carry free LLM text; metadata frames (routing,
        # sources, facts, places, images) must stay byte-identical. A new frame
        # object replaces the original so upstream payloads are never mutated.
        if event == SSE_EVENT_TOKEN and isinstance(data.get("text"), str):
            released = self._streaming_sanitizer.feed(data["text"])
            if released:
                self._stream_released.append(released)
            data = {**data, "text": released}
        return self.on_event(event, data)

    async def _flush_stream_tail(self) -> str:
        """Release the hold-back tail as one final token frame (Bug A).

        Called exactly once per run after the inner workflow settles — normal,
        timed-out, aborted, or failed. Without this the last ~35 buffered
        characters (plus any unterminated-bracket span) never reach the stream
        and, when done also fails to arrive, are lost to the user mid-word.
        """
        tail = self._streaming_sanitizer.flush()
        if not tail:
            return ""
        self._stream_released.append(tail)
        await self._emit(SSE_EVENT_TOKEN, {"text": tail})
        return tail

    async def _emit(self, event: str, data: dict) -> None:
        import inspect

        res = self.on_event(event, data)
        if inspect.isawaitable(res):
            await res

    @step()
    async def start_conv(self, ctx: Context, ev: StartEvent) -> ConvRunEv:
        """Conv layer: context -> slots -> transition -> routing event (before legs)."""
        query = ev.query
        session_id = getattr(ev, "session_id", None) or "anon"
        # Fresh hold-back state per run: a rerun on the same instance must not
        # inherit an unterminated-bracket tail from the previous answer.
        self._streaming_sanitizer.reset()
        self._stream_released = []
        history = getattr(ev, "history", None) or []
        as_of = getattr(ev, "as_of", None)
        project_context: ProjectContext = getattr(ev, "project_context", None)
        if not isinstance(project_context, ProjectContext):
            raise ValueError("project_context is required")
        project_key = project_context.project_key
        device_id = getattr(ev, "device_id", None)
        visible_limit = getattr(ev, "visible_limit", None)
        conv = await _extract_and_transition(query, session_id, history, project_key, device_id)
        await ctx.store.set("conv", conv)
        # CTA hint gated §6.5: (d) uses the PREVIOUS answer review status
        # (routing emits before legs; first turn treats None as clean).
        requires_review = bool(conv["slots"].get("last_answer_reviewed"))
        cta = maybe_lead_cta_hint(
            get_context(session_id, device_id), requires_review=requires_review
        )
        conv["lead_cta_hint"] = cta
        t0 = time.perf_counter()
        payload = {
            "intent": conv["intent"],
            "conv_state": conv["conv_state"],
            "panel_hint": _panel_hint(Intent(conv["intent"])),
            "lead_cta_hint": cta,
        }
        await self._emit(SSE_EVENT_ROUTING, payload)
        return ConvRunEv(
            conv=conv,
            query=query,
            session_id=session_id,
            as_of=as_of,
            history=history,
            project_context=project_context,
            device_id=device_id,
            visible_limit=visible_limit,
        )

    @step()
    async def run_inner(self, ctx: Context, ev: ConvRunEv) -> StopEvent:
        """Run the inner 8-step workflow to completion; decorate result with conv meta.

        Bug A contract: the sanitizer hold-back tail is flushed on EVERY exit
        path, and when the inner run dies after tokens were already streamed,
        this step degrades to a best-effort `done` payload (sanitized partial +
        ``answer_truncated`` flag) instead of raising — otherwise main.py's
        crash path emits an answerless `done` and the FE keeps mid-word text.
        """
        conv = ev.conv
        handler = self._inner.run(
            query=ev.query,
            session_id=ev.session_id,
            as_of=ev.as_of,
            history=ev.history or [],
            project_context=ev.project_context,
            device_id=ev.device_id,
            visible_limit=getattr(ev, "visible_limit", None),
        )
        result: Any = None
        run_error: Exception | None = None
        tasks_before = len(asyncio.all_tasks()) if os.getenv("RAG_TASK_DEBUG") == "1" else None
        try:
            result = await handler
        except asyncio.CancelledError:
            # A cancelled outer request must also stop the inner workflow and
            # wait for its task to settle, otherwise the event loop reports a
            # destroyed pending task during shutdown. Shield the cleanup so
            # cancellation of this task cannot interrupt the settlement await.
            handler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(handler)
            raise
        except QueryRejected:
            # L1 rejections carry no streamed tokens; keep the SSE layer's
            # error+done contract untouched.
            raise
        except Exception as exc:  # noqa: BLE001 — degrade to partial-answer done
            run_error = exc
        finally:
            if tasks_before is not None:
                logger.debug(
                    "rag task lifecycle: before=%d after=%d delta=%d",
                    tasks_before,
                    len(asyncio.all_tasks()),
                    len(asyncio.all_tasks()) - tasks_before,
                )
        await self._flush_stream_tail()
        if run_error is not None:
            if not any(self._stream_released):
                # Nothing streamed — there is no partial answer to preserve, so
                # surface the failure through the normal error frame instead.
                raise run_error
            logger.error(
                "conv inner run failed after %d released batches: %s: %s",
                len(self._stream_released),
                type(run_error).__name__,
                run_error,
            )
            result = {
                "answer": "".join(self._stream_released),
                "answer_truncated": True,
                "confidence": "LOW",
                "requires_review": True,
                "sources": [],
                "facts": [],
                "images": [],
                "places": [],
                "routing": {},
                "trace_id": "",
                "latency_ms": 0,
                # Stable, non-leaky shape mirroring main.py's SSE crash frame;
                # exception class/text stay server-side (logged above) only.
                "error": {"code": "INTERNAL", "message": "internal error"},
            }
        # Persist the review status + useful-turn flag for the NEXT routing event.
        sctx = get_context(ev.session_id, ev.device_id)
        if isinstance(result, dict):
            # Final post-processing step (spec §8): the done payload replaces
            # the streamed body client-side, so it must be re-sanitized even if
            # a marker shape slipped past the streaming hold-back guards.
            # terminated=True: the answer is complete, so an unterminated "["
            # group or a dangling key stem is judged as-is and removed.
            if result.get("answer"):
                result["answer"] = sanitize_answer_text(result["answer"], terminated=True)
            sctx.slots["last_answer_reviewed"] = bool(result.get("requires_review", False))
            if not result.get("requires_review", False) and result.get("answer"):
                note_useful_turn(sctx)
            result["conv_state"] = conv["conv_state"]
            # FR-25 (revised): the sales funnel metadata (persona directive +
            # slot prefill + CTA hint) must never decorate a training payload
            # (lookup assistant voice) — normal turns are byte-unchanged.
            if ev.project_context.answer_mode == "training":
                result["conversation_directive"] = None
                result["slots_prefill"] = None
                result["lead_cta_hint"] = None
            else:
                result["conversation_directive"] = conv["directive"]
                result["slots_prefill"] = conv["prefill_note"]
                result["lead_cta_hint"] = conv.get("lead_cta_hint")
        else:
            result = {"conv_state": conv["conv_state"], "conversation_directive": conv["directive"]}
        return StopEvent(result=result)


class RagQueryPipelineConv:
    """Conv-capable facade — same contract as RagQueryPipeline, /query default."""

    def __init__(self, on_event: EventCallback | None = None):
        self._on_event = on_event

    async def run(
        self,
        query: str,
        session_id: str | None = None,
        as_of: str | None = None,
        history: list[dict] | None = None,
        *,
        project_context: ProjectContext,
        device_id: str | None = None,
        on_event: EventCallback | None = None,
        visible_limit: int | None = None,
    ) -> dict:
        from api.application.services.project_config import (  # noqa: PLC0415
            bound_request_project,
            load_project_registry_record,
        )

        record = await load_project_registry_record(project_context.project_key)
        with bound_request_project(record):
            wf = RagRgreConvWorkflow(on_event=on_event if on_event is not None else self._on_event)
            return await wf.run(
                query=query,
                session_id=session_id,
                as_of=as_of,
                history=history or [],
                project_context=project_context,
                device_id=device_id,
                visible_limit=visible_limit,
            )

async def build_project_redirect_payload(
    query: str,
    session_id: str | None,
    history: list[dict] | None,
    *,
    project_context: ProjectContext,
    device_id: str | None,
    project_redirect: dict[str, str],
) -> tuple[dict, dict]:
    """Build the existing redirect result without invoking retrieval.

    `/query` owns detection and calls this only after resolving its pinned
    project but before quota reservation. Keeping the conversation transition
    here preserves the session state/CTA behavior of a normal redirect turn.
    """
    if not isinstance(project_context, ProjectContext):
        raise ValueError("project_context is required")
    from api.application.services.project_config import (  # noqa: PLC0415
        bound_request_project,
        load_project_registry_record,
    )
    from api.domain.services.project_redirect import redirect_message  # noqa: PLC0415

    record = await load_project_registry_record(project_context.project_key)
    session_key = session_id or "anon"
    with bound_request_project(record):
        conv = await _extract_and_transition(
            query, session_key, history or [], project_context.project_key, device_id
        )
    context = get_context(session_key, device_id)
    requires_review = bool(conv["slots"].get("last_answer_reviewed"))
    cta = maybe_lead_cta_hint(context, requires_review=requires_review)
    note_useful_turn(context)
    routing = {
        "intent": conv["intent"],
        "conv_state": conv["conv_state"],
        "panel_hint": _panel_hint(Intent(conv["intent"])),
        "lead_cta_hint": cta,
        "project_redirect": project_redirect,
    }
    payload = {
        "answer": redirect_message(project_redirect["display_name"]),
        "sources": [],
        "facts": [],
        "images": [],
        "videos": [],
        "places": [],
        "confidence": "HIGH",
        "requires_review": False,
        "routing": {"intent": conv["intent"]},
        "trace_id": "",
        "latency_ms": 0,
        "conv_state": conv["conv_state"],
        "conversation_directive": conv["directive"],
        "slots_prefill": conv["prefill_note"],
        "lead_cta_hint": cta,
        "project_redirect": project_redirect,
        "status": "completed",
        "history_persisted": False,
        "quota": None,
    }
    return payload, routing



__all__ = [
    "RagRgreConvWorkflow",
    "RagQueryPipelineConv",
    "SSE_EVENT_ROUTING",
    "_panel_hint",
]
