"""LlamaIndex Workflows orchestrator for the 8-step query pipeline (AD-18).

Flow: guard -> rewrite/route -> RAG + SQL + geo legs in parallel -> rerank ->
merge (SSE: places -> sources -> facts) -> generate (stream) -> output guard ->
audit. Every step degrades gracefully on timeout/error instead of crashing.

Cross-step data lives in ``ctx.store`` (DictState); events are routing signals
only, so the step graph stays a pure DAG.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)

from api import get_cfg, sha256_hex
from api.audit import write_audit
from api.constants import (
    SSE_EVENT_FACTS,
    SSE_EVENT_PLACES,
    SSE_EVENT_SOURCES,
    SSE_EVENT_TOKEN,
)
from api.dependencies import get_geo, get_reranker
from api.generate import stream_answer
from api.guard_input import GuardResult as InputGuardResult
from api.guard_input import guard_input
from api.guard_output import GuardResult as OutputGuardResult
from api.guard_output import guard_output
from api.merge import Merged, merge_context
from api.ports.geo import GeoResult
from api.rag_leg import RagLegResult, run_rag_leg
from api.rewrite import RoutedResult, fallback_route, rewrite_query
from api.sql_leg import SqlLegResult, run_sql_leg

logger = logging.getLogger("api.workflow")

STEP_TIMEOUTS = {
    "guard": 1.0,
    "rewrite": 4.0,
    "rag": 6.0,
    "sql": 2.0,
    "sql_nl2sql": 8.0,
    "rerank": 3.0,
    "geo": 3.0,
    "output_guard": 2.0,
}

# Event callback: accepts async or sync callables (workflow awaits coroutines).
EventCallback = Callable[[str, dict], Awaitable[None] | None]


class QueryRejected(Exception):
    """L1 rejection — main maps this to HTTP 400 + audit."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"rejected: {reason}")


def parse_as_of(value: str | None) -> date | None:
    """Parse 'YYYY-MM-DD' to a date; None/invalid -> None (means today)."""
    if not value:
        return None
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        logger.warning("as_of invalid (%r) — defaulting to today", value)
        return None


# --- Workflow events (signals only) -------------------------------------------
class GuardedEv(Event):
    """L1 passed — carry the cleaned query to the router."""

    clean: str = ""


class RagRequestEv(Event):
    pass


class SqlRequestEv(Event):
    pass


class GeoRequestEv(Event):
    pass


class RagDoneEv(Event):
    pass


class SqlDoneEv(Event):
    pass


class GeoDoneEv(Event):
    pass


class MergedEv(Event):
    pass


class GeneratedEv(Event):
    pass


class RagQueryWorkflow(Workflow):
    """LlamaIndex Workflows re-implementation of the 8-step pipeline (AD-18)."""

    def __init__(self, timeout: float = 45.0, on_event: EventCallback | None = None):
        super().__init__(timeout=timeout)
        self.on_event: EventCallback = on_event or (lambda event, data: None)

    # --- shared-state helpers --------------------------------------------------
    async def _emit(self, event: str, data: dict) -> None:
        res = self.on_event(event, data)
        if inspect.isawaitable(res):
            await res

    async def _flag(self, ctx: Context, *flags: str) -> None:
        """Append degradation flags (dedup) to shared state, atomically."""
        async with ctx.store.edit_state() as state:
            degraded = list(state.get("degraded", []) or [])
            for f in flags:
                if f and f not in degraded:
                    degraded.append(f)
            state["degraded"] = degraded

    async def _write_audit(self, ctx: Context) -> None:
        """Audit is append-only and never fails the pipeline."""
        try:
            await write_audit(await ctx.store.get("audit"))
        except Exception:  # noqa: BLE001 — audit failure never crashes
            logger.exception("audit write failed (ignored)")

    # --- steps -----------------------------------------------------------------
    @step()
    async def guard(self, ctx: Context, ev: StartEvent) -> GuardedEv:
        t0 = time.perf_counter()
        trace_id = "t-" + uuid.uuid4().hex[:10]
        session_id = getattr(ev, "session_id", None)
        await ctx.store.set("trace_id", trace_id)
        await ctx.store.set("t0", t0)
        await ctx.store.set("query", ev.query)
        await ctx.store.set("session_id", session_id)
        await ctx.store.set("as_of_date", parse_as_of(getattr(ev, "as_of", None)))
        await ctx.store.set("history", getattr(ev, "history", None) or [])
        await ctx.store.set("degraded", [])
        await ctx.store.set(
            "audit",
            {"trace_id": trace_id, "session_id": session_id, "query": ev.query, "latency_ms": None},
        )

        try:
            guard = await asyncio.wait_for(guard_input(ev.query), timeout=STEP_TIMEOUTS["guard"])
        except asyncio.TimeoutError:
            guard = InputGuardResult(clean=ev.query, degraded=True)
            await self._flag(ctx, "guard_timeout")
        if guard.rejected:
            audit = await ctx.store.get("audit")
            audit["guard_verdicts"] = {"L1": "reject", "reason": guard.reason}
            await ctx.store.set("audit", audit)
            await self._write_audit(ctx)
            raise QueryRejected(guard.reason or "L1 rejected")
        if guard.degraded:
            await self._flag(ctx, "guard_rule_only")
        await ctx.store.set("guard", guard)
        return GuardedEv(clean=guard.clean)

    @step()
    async def route(
        self, ctx: Context, ev: GuardedEv
    ) -> RagRequestEv | SqlRequestEv | GeoRequestEv | None:
        as_of = await ctx.store.get("as_of_date")
        as_of_iso = as_of.isoformat() if as_of else None
        try:
            routed = await asyncio.wait_for(
                rewrite_query(ev.clean, await ctx.store.get("history"), as_of_iso),
                timeout=STEP_TIMEOUTS["rewrite"],
            )
        except asyncio.TimeoutError:
            routed = fallback_route(ev.clean, as_of_iso, "rewrite_timeout")
            await self._flag(ctx, "rewrite_timeout")
        except Exception as exc:  # noqa: BLE001 — router failure falls back to rag-only
            routed = fallback_route(ev.clean, as_of_iso, f"rewrite_error:{exc}")
            await self._flag(ctx, "rewrite_error")
        await self._flag(ctx, *routed.degraded)
        await ctx.store.set("routed", routed)

        audit = await ctx.store.get("audit")
        audit.update(
            rewritten_query=routed.rewritten,
            routing=routed.routing,
            structured_path=routed.routing.get("structured_path"),
            sql_spec=routed.sql_spec or None,
        )
        await ctx.store.set("audit", audit)

        # Fan out all three legs. Each leg self-gates on its routing flag, so
        # merge always collects the same three done events (no conditional join).
        ctx.send_event(RagRequestEv())
        ctx.send_event(SqlRequestEv())
        ctx.send_event(GeoRequestEv())
        return None

    @step()
    async def rag_leg(self, ctx: Context, ev: RagRequestEv) -> RagDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_rag", True):
            await ctx.store.set("rag_result", RagLegResult([], degraded=False))
            return RagDoneEv()
        try:
            result = await asyncio.wait_for(
                run_rag_leg(
                    routed.rewritten,
                    routed.hl_keywords,
                    routed.ll_keywords,
                    await ctx.store.get("as_of_date"),
                ),
                timeout=STEP_TIMEOUTS["rag"],
            )
        except asyncio.TimeoutError:
            result = RagLegResult([], degraded=True, error="timeout")
            await self._flag(ctx, "rag_timeout")
        except Exception as exc:  # noqa: BLE001 — leg failure degrades, never crashes
            result = RagLegResult([], degraded=True, error=str(exc))
            await self._flag(ctx, f"rag_error:{exc}")
        if result.degraded:
            await self._flag(ctx, f"rag_degraded:{result.error or ''}")
        await ctx.store.set("rag_result", result)
        return RagDoneEv()

    @step()
    async def sql_leg(self, ctx: Context, ev: SqlRequestEv) -> SqlDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_sql", False):
            await ctx.store.set("sql_result", SqlLegResult([], {"mode": "none"}, degraded=False))
            return SqlDoneEv()
        guard: InputGuardResult = await ctx.store.get("guard")
        spec = routed.sql_spec or {}
        if routed.routing.get("structured_path") == "nl2sql":
            spec = dict(spec)
            spec["structured_path"] = "nl2sql"
        timeout = (
            STEP_TIMEOUTS["sql_nl2sql"]
            if routed.routing.get("structured_path") == "nl2sql"
            else STEP_TIMEOUTS["sql"]
        )
        try:
            result = await asyncio.wait_for(
                run_sql_leg(spec, await ctx.store.get("as_of_date"), guard.clean),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            result = SqlLegResult([], {"mode": "spec", "error": "timeout"}, degraded=True)
            await self._flag(ctx, "sql_timeout")
        except Exception as exc:  # noqa: BLE001 — leg failure degrades, never crashes
            result = SqlLegResult([], {"mode": "spec", "error": str(exc)}, degraded=True)
            await self._flag(ctx, f"sql_error:{exc}")
        if result.degraded:
            await self._flag(ctx, f"sql_degraded:{result.meta.get('error') or ''}")
        await ctx.store.set("sql_result", result)
        return SqlDoneEv()

    @step()
    async def geo_leg(self, ctx: Context, ev: GeoRequestEv) -> GeoDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_geo", False):
            await ctx.store.set("geo_result", GeoResult([], degraded=False))
            return GeoDoneEv()
        try:
            result = await asyncio.wait_for(
                get_geo().places_around(
                    get_cfg("geo_center_lat", 16.0558),
                    get_cfg("geo_center_lng", 108.2455),
                    get_cfg("geo_radius_m", 10000),
                ),
                timeout=STEP_TIMEOUTS["geo"],
            )
        except asyncio.TimeoutError:
            result = GeoResult([], degraded=True, error="timeout")
            await self._flag(ctx, "geo_timeout")
        except Exception as exc:  # noqa: BLE001 — geo failure degrades, never crashes
            result = GeoResult([], degraded=True, error=str(exc))
            await self._flag(ctx, f"geo_error:{exc}")
        if result.degraded:
            await self._flag(ctx, f"geo_degraded:{result.error or ''}")
        await ctx.store.set("geo_result", result)
        return GeoDoneEv()

    @step()
    async def merge(self, ctx: Context, ev: RagDoneEv | SqlDoneEv | GeoDoneEv) -> MergedEv:
        done = ctx.collect_events(ev, [RagDoneEv, SqlDoneEv, GeoDoneEv])
        if done is None:
            return None
        guard: InputGuardResult = await ctx.store.get("guard")
        routed: RoutedResult = await ctx.store.get("routed")
        rag_result: RagLegResult = await ctx.store.get("rag_result")
        sql_result: SqlLegResult = await ctx.store.get("sql_result")
        geo_result = await ctx.store.get("geo_result")
        as_of = await ctx.store.get("as_of_date")

        # App-side rerank is the single score source for confidence.
        chunks = rag_result.chunks
        try:
            chunks = await asyncio.wait_for(
                get_reranker().rerank(routed.rewritten, chunks), timeout=STEP_TIMEOUTS["rerank"]
            )
        except asyncio.TimeoutError:
            await self._flag(ctx, "rerank_timeout")
        except Exception as exc:  # noqa: BLE001
            await self._flag(ctx, f"rerank_error:{exc}")
        if any(c.get("_rerank_degraded") or c.get("_rerank_off") for c in chunks):
            await self._flag(ctx, "rerank_degraded")
        await ctx.store.set("reranked_chunks", chunks)

        merged: Merged = await merge_context(guard.clean, chunks, sql_result.rows, as_of)
        merged.meta.update(
            query=guard.clean,
            rewritten=routed.rewritten,
            as_of=as_of.isoformat() if as_of else None,
            degraded=await ctx.store.get("degraded"),
            sql_row_count=len(sql_result.rows),
            has_approx=any(e.get("quality") in ("range", "approx") for e in sql_result.rows),
            strong_chunks=sum(1 for c in chunks if float(c.get("score", 0.0)) >= 0.8),
        )
        await ctx.store.set("merged", merged)

        audit = await ctx.store.get("audit")
        audit.update(
            sql_query=sql_result.meta.get("sql_query"),
            fact_ids=[e.get("fact_id") for e in sql_result.rows if e.get("fact_id")],
            chunk_ids=[c.get("id") for c in rag_result.chunks if c.get("id")],
            rerank_scores=[c.get("score") for c in chunks],
        )
        await ctx.store.set("audit", audit)

        # SSE: places first, then sources, then facts (event order in the SS contract).
        if routed.routing.get("needs_geo", False):
            await self._emit(SSE_EVENT_PLACES, {"places": _places_payload(geo_result)})
        await self._emit(SSE_EVENT_SOURCES, {"sources": merged.sources})
        await self._emit(SSE_EVENT_FACTS, {"facts": merged.facts})
        return MergedEv()

    @step()
    async def generate(self, ctx: Context, ev: MergedEv) -> GeneratedEv:
        merged: Merged = await ctx.store.get("merged")
        routed: RoutedResult = await ctx.store.get("routed")
        parts: list[str] = []
        async for token in stream_answer(
            merged, await ctx.store.get("history"), routed.high_stakes
        ):
            parts.append(token)
            await self._emit(SSE_EVENT_TOKEN, {"text": token})
        answer = "".join(parts)
        await ctx.store.set("answer", answer)

        audit = await ctx.store.get("audit")
        audit.update(
            model=merged.meta.get("model"),
            prompt_hash=merged.meta.get("prompt_hash"),
            answer_hash=sha256_hex(answer),
        )
        await ctx.store.set("audit", audit)
        return GeneratedEv()

    @step()
    async def output_guard(self, ctx: Context, ev: GeneratedEv) -> StopEvent:
        merged: Merged = await ctx.store.get("merged")
        answer: str = await ctx.store.get("answer")
        routed: RoutedResult = await ctx.store.get("routed")
        try:
            guard_res = await asyncio.wait_for(
                guard_output(
                    answer, merged.facts, merged.sources, routed.routing, meta=merged.meta
                ),
                timeout=STEP_TIMEOUTS["output_guard"],
            )
        except asyncio.TimeoutError:
            guard_res = OutputGuardResult(
                confidence="MEDIUM", requires_review=False, verdicts={"timeout": True}
            )
            await self._flag(ctx, "output_guard_timeout")

        audit = await ctx.store.get("audit")
        audit.update(confidence=guard_res.confidence, guard_verdicts=guard_res.verdicts)
        latency_ms = int((time.perf_counter() - await ctx.store.get("t0")) * 1000)
        audit.update(latency_ms=latency_ms, degraded=await ctx.store.get("degraded"))
        await ctx.store.set("audit", audit)
        await self._write_audit(ctx)

        return StopEvent(
            result={
                "answer": answer,
                "sources": merged.sources,
                "facts": merged.facts,
                "places": _places_payload(await ctx.store.get("geo_result"))
                if routed.routing.get("needs_geo", False)
                else [],
                "confidence": guard_res.confidence,
                "requires_review": guard_res.requires_review,
                "routing": routed.routing,
                "trace_id": await ctx.store.get("trace_id"),
                "latency_ms": latency_ms,
            }
        )


class RagQueryPipeline:
    """Back-compat facade: `await run(**kwargs) -> dict` over the workflow.

    Keeps eval/run_eval.py and main.py's existing call contract; the workflow
    emits SSE events through the optional on_event callback during steps.
    """

    def __init__(self, on_event: EventCallback | None = None):
        self._on_event = on_event

    async def run(
        self,
        query: str,
        session_id: str | None = None,
        as_of: str | None = None,
        history: list[dict] | None = None,
        on_event: EventCallback | None = None,
    ) -> dict:
        wf = RagQueryWorkflow(on_event=on_event if on_event is not None else self._on_event)
        handler = wf.run(query=query, session_id=session_id, as_of=as_of, history=history or [])
        return await handler


def _places_payload(result: GeoResult | None) -> list[dict[str, Any]]:
    """GeoResult -> JSON-safe place list for SSE events + maps rendering."""
    if result is None:
        return []
    return [
        {
            "name": p.name,
            "kinds": list(p.kinds),
            "lat": p.lat,
            "lng": p.lng,
            "distance_m": p.distance_m,
            "address": p.address,
            "rating": p.rating,
        }
        for p in result.places
    ]
