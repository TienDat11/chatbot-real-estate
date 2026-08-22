"""Conversation engine shell (Story 4.5, §6.1).

RagRgreConvWorkflow wraps RagQueryWorkflow WITHOUT forking its 8 steps: the
conv layer (ConvContext load -> slot extract -> transition -> routing SSE
event -> CTA hint) runs as a thin LlamaIndex Workflow with 2 steps; the inner
workflow runs to completion as a coroutine from the second step.

The routing SSE event is emitted BEFORE the legs start (§6.6 "emit trước legs")
with intent/conv_state/panel_hint + lead_cta_hint gated by §6.5 (a)-(e).
RagQueryPipeline facade keeps its exact contract so eval/main keep working;
main.py /query is switched to the conv path (back-compat kept).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)

from api.application.services.conv_state import (
    conv_directive,
    get_context,
    maybe_lead_cta_hint,
    note_useful_turn,
    register_interest,
    transition,
)
from api.application.services.project_config import brand_token
from api.domain.services.conv_slots import extract_slots, lead_prefill_note
from api.domain.services.route_intent import Intent, classify_intent
from api.application.pipelines.workflow import (
    EventCallback,
    RagQueryWorkflow,
)

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
    query: str, session_id: str, history: list[dict] | None,
    project_key: str | None, device_id: str | None,
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
    session_id: "str | None"
    as_of: "str | None"
    history: "list[dict] | None"
    project_key: "str | None"
    device_id: "str | None"


class RagRgreConvWorkflow(Workflow):
    """Thin conv shell over RagQueryWorkflow (DO NOT fork the 8 steps)."""

    def __init__(self, timeout: float = 180.0, on_event: "EventCallback | None" = None):
        super().__init__(timeout=timeout)
        self._inner = RagQueryWorkflow(timeout=timeout, on_event=on_event)
        self.on_event: EventCallback = on_event or (lambda event, data: None)

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
        history = getattr(ev, "history", None) or []
        as_of = getattr(ev, "as_of", None)
        project_key = getattr(ev, "project_key", None)
        device_id = getattr(ev, "device_id", None)
        conv = await _extract_and_transition(query, session_id, history, project_key, device_id)
        await ctx.store.set("conv", conv)
        # CTA hint gated §6.5: (d) uses the PREVIOUS answer review status
        # (routing emits before legs; first turn treats None as clean).
        requires_review = bool(conv["slots"].get("last_answer_reviewed"))
        cta = maybe_lead_cta_hint(get_context(session_id, device_id), requires_review=requires_review)
        payload = {
            "intent": conv["intent"],
            "conv_state": conv["conv_state"],
            "panel_hint": _panel_hint(Intent(conv["intent"])),
            "lead_cta_hint": cta,
        }
        await self._emit(SSE_EVENT_ROUTING, payload)
        return ConvRunEv(
            conv=conv, query=query, session_id=session_id, as_of=as_of, history=history,
            project_key=project_key, device_id=device_id,
        )

    @step()
    async def run_inner(self, ctx: Context, ev: ConvRunEv) -> StopEvent:
        """Run the inner 8-step workflow to completion; decorate result with conv meta."""
        conv = ev.conv
        handler = self._inner.run(
            query=ev.query,
            session_id=ev.session_id,
            as_of=ev.as_of,
            history=ev.history or [],
            project_key=ev.project_key,
            device_id=ev.device_id,
        )
        result = await handler
        # Persist the review status + useful-turn flag for the NEXT routing event.
        sctx = get_context(ev.session_id, ev.device_id)
        if isinstance(result, dict):
            sctx.slots["last_answer_reviewed"] = bool(result.get("requires_review", False))
            if not result.get("requires_review", False) and result.get("answer"):
                note_useful_turn(sctx)
            result["conv_state"] = conv["conv_state"]
            result["conversation_directive"] = conv["directive"]
            result["slots_prefill"] = conv["prefill_note"]
        else:
            result = {"conv_state": conv["conv_state"], "conversation_directive": conv["directive"]}
        return StopEvent(result=result)


class RagQueryPipelineConv:
    """Conv-capable facade — same contract as RagQueryPipeline, /query default."""

    def __init__(self, on_event: "EventCallback | None" = None):
        self._on_event = on_event

    async def run(
        self,
        query: str,
        session_id: "str | None" = None,
        as_of: "str | None" = None,
        history: "list[dict] | None" = None,
        project_key: "str | None" = None,
        device_id: "str | None" = None,
        on_event: "EventCallback | None" = None,
    ) -> dict:
        wf = RagRgreConvWorkflow(on_event=on_event if on_event is not None else self._on_event)
        return await wf.run(
            query=query, session_id=session_id, as_of=as_of, history=history or [],
            project_key=project_key, device_id=device_id,
        )


__all__ = [
    "RagRgreConvWorkflow", "RagQueryPipelineConv", "SSE_EVENT_ROUTING", "_panel_hint",
]
