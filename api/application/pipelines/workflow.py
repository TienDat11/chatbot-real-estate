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

from api import get_cfg
from api.application.services.audit import write_audit
from api.application.services.conv_state import conv_directive, get_context
from api.application.services.generate import stream_answer
from api.application.services.image_search import (
    fetch_price_board_images,
    is_full_price_board_query,
    search_images,
)
from api.application.services.merge import Merged, merge_context
from api.application.services.project_config import request_project_snapshot
from api.application.services.project_scope import is_training_scope
from api.application.services.rag_leg import RagLegResult, run_rag_leg
from api.application.services.sql_leg import SqlLegResult, run_sql_leg
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_input import guard_input, rule_screen
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.guard_output import guard_output, sanitize_output
from api.domain.services.rewrite import RoutedResult, fallback_route, rewrite_query
from api.domain.services.utils import sha256_hex
from api.domain.value_objects.constants import (
    SSE_EVENT_FACTS,
    SSE_EVENT_IMAGES,
    SSE_EVENT_PLACES,
    SSE_EVENT_SOURCES,
    SSE_EVENT_TOKEN,
)
from api.infrastructure.config.config import project_geo_center
from api.infrastructure.dependencies import get_geo, get_reranker
from api.domain.services.guard_output import (
    GuardResult as OutputGuardResult,
    guard_output,
    normalize_answer_display,
    sanitize_output,
)
from api.infrastructure.ports.geo import GeoResult

logger = logging.getLogger("api.workflow")

# Per-step budgets (asyncio.wait_for). Measured warm latencies (scripts/timing
# probe): guard 0.1ms, rewrite <=9.3s (single call), rag <=5.9s, sql ~0.15s,
# nl2sql ~8s, rerank 0.3-0.5s, geo 0.5ms, output_guard 0.2-4ms, images ~0.3s.
# Each value keeps >=1.3x headroom over the observed happy-path time so normal
# requests are never cut, while still bounding stuck calls so the workflow
# degrades to its fallback instead of parking. nl2sql 11.0: measured happy-path
# runs ~8s, so 11.0 leaves >=1.3x headroom (8.0 sat the budget and timed out real
# requests). sql 3.0: run_sql_leg runs two sequential with_rls_identity(timeout_s=1.5)
# blocks, so the outer budget must cover their combined 3.0s so the inner
# statement_timeout fires before the workflow cuts. geo 3.0: covers the
# google_places HTTP TIMEOUT_S of 2.5s.
STEP_TIMEOUTS = {
    "guard": 1.0,
    "rewrite": 12.0,
    "rag": 25.0,
    "sql": 3.0,
    "sql_nl2sql": 11.0,
    "rerank": 3.0,
    "geo": 3.0,
    "output_guard": 1.5,
}

# Event callback: accepts async or sync callables (workflow awaits coroutines).
EventCallback = Callable[[str, dict], Awaitable[None] | None]


class QueryRejected(Exception):
    """L1 rejection — main maps this to HTTP 400 + audit."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"rejected: {reason}")


def training_retrieval_scope(
    project_key: str | None, training_context: str | None
) -> str | None:
    """Corpus scope every training leg must read: the REAL context project.

    By business decision training and customer chat share ONE project corpus
    (documents.kind is only ever legal|price|project), so ``_training`` is a
    prompt-profile marker only and must never reach a retrieval filter — a
    training turn scoped to no real project retrieves nothing (project
    isolation is absolute).
    """
    if not is_training_scope(project_key):
        return project_key
    return training_context


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

    def __init__(self, timeout: float = 180.0, on_event: EventCallback | None = None):
        super().__init__(timeout=timeout)
        self.on_event: EventCallback = on_event or (lambda event, data: None)

    # --- shared-state helpers --------------------------------------------------
    async def _emit(self, event: str, data: dict) -> None:
        res = self.on_event(event, data)
        if inspect.isawaitable(res):
            await res

    async def _store_get(self, ctx: Context, key: str):
        """Read one store key, returning None when the step runs standalone.

        The guard step seeds most keys, but steps are also invoked directly in
        tests with a partial store (e.g. wf.sql_leg(ctx, SqlRequestEv())); a
        missing key must read as absent. The state store documents ValueError
        as its missing-path error, so only that type is treated as "absent" —
        a genuine store failure (serialization, IO) must surface loudly (M7).
        """
        try:
            return await ctx.store.get(key)
        except ValueError:  # "Path not found in state" — the documented miss
            return None

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
        await ctx.store.set("project_key", getattr(ev, "project_key", None))
        # G3-r6: optional context project for training turns (None everywhere
        # else, so normal-mode state is byte-identical).
        await ctx.store.set(
            "training_context_project_key", getattr(ev, "training_context_project_key", None)
        )
        await ctx.store.set("device_id", getattr(ev, "device_id", None))
        await ctx.store.set("as_of_date", parse_as_of(getattr(ev, "as_of", None)))
        await ctx.store.set("degraded", [])
        # Screen every user history turn with the same rule set as the query (L1);
        # stored history is re-embedded verbatim into rewrite/generate prompts, so
        # an injection smuggled via history must not reach the model.
        history = getattr(ev, "history", None) or []
        for turn in history:
            if turn.get("role") == "user":
                reason = rule_screen(turn["content"])
                if reason:
                    raise QueryRejected(f"L1 history: {reason}")
        await ctx.store.set("history", history)
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
        project_key = await self._store_get(ctx, "project_key")
        training_context = await self._store_get(ctx, "training_context_project_key")
        # Fail closed (FR-25 revision): an unresolved training turn must NEVER
        # reach run_rag_leg with project_key=None — rag_leg._post_filter drops
        # its `d.project_key = $2` predicate on None, which would retrieve the
        # FULL corpus (every project's chunks together). Customer mode may
        # legitimately pass None per its own scoping contract (single-active
        # default rule, story 10.1), so this guard is training-only and mirrors
        # the sql_leg / geo_leg fail-closed shape: loud degrade flag, zero
        # retrieval, no data from any project.
        if is_training_scope(project_key) and not training_context:
            await ctx.store.set(
                "rag_result",
                RagLegResult(
                    [],
                    degraded=True,
                    error="training_scope_unresolved",
                    degraded_reasons=("training_scope_unresolved",),
                ),
            )
            await self._flag(ctx, "training_scope_unresolved")
            return RagDoneEv()
        try:
            result = await asyncio.wait_for(
                run_rag_leg(
                    routed.rewritten,
                    routed.hl_keywords,
                    routed.ll_keywords,
                    await ctx.store.get("as_of_date"),
                    # Shared corpus (FR-25 revision): a training turn retrieves
                    # from its real context project exactly like a customer turn;
                    # no doc-kind filter exists any more. The training path can
                    # only get here with a resolved real project (guard above),
                    # so None below always means customer mode, never training.
                    training_retrieval_scope(project_key, training_context),
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
            # Preserve the leg's auditable reason codes (for example,
            # ``aquery_timeout``) instead of collapsing them into an opaque
            # error string. Keep the legacy marker only when no structured
            # reason was supplied by the leg.
            await self._flag(ctx, *result.degraded_reasons)
            if not result.degraded_reasons:
                await self._flag(ctx, f"rag_degraded:{result.error or ''}")
        await ctx.store.set("rag_result", result)
        return RagDoneEv()

    @step()
    async def sql_leg(self, ctx: Context, ev: SqlRequestEv) -> SqlDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_sql", False):
            await ctx.store.set("sql_result", SqlLegResult([], {"mode": "none"}, degraded=False))
            return SqlDoneEv()
        # Shared corpus (FR-25 revision): structured facts ARE project data,
        # so a training turn runs the SQL leg against its real context project
        # like any customer turn. The marker key must never reach the scope
        # filter (facts are tagged with real project keys), so an unscoped
        # training turn skips the leg loudly instead of silently reading wrong.
        project_key = await self._store_get(ctx, "project_key")
        training_context = await self._store_get(ctx, "training_context_project_key")
        if is_training_scope(project_key) and not training_context:
            await ctx.store.set(
                "sql_result",
                SqlLegResult([], {"mode": "none", "error": "training_scope_unresolved"}, degraded=True),
            )
            await self._flag(ctx, "training_scope_unresolved")
            return SqlDoneEv()
        sql_scope = training_retrieval_scope(project_key, training_context)
        guard: InputGuardResult = await ctx.store.get("guard")
        spec = routed.sql_spec or {}
        if routed.routing.get("structured_path") == "nl2sql":
            spec = dict(spec)
            spec["structured_path"] = "nl2sql"
        elif routed.routing.get("structured_path") == "affordability":
            spec = dict(spec)
            spec["structured_path"] = "affordability"
        timeout = (
            STEP_TIMEOUTS["sql_nl2sql"]
            if routed.routing.get("structured_path") == "nl2sql"
            else STEP_TIMEOUTS["sql"]
        )
        try:
            result = await asyncio.wait_for(
                run_sql_leg(
                    spec,
                    await ctx.store.get("as_of_date"),
                    guard.clean,
                    sql_scope,
                ),
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
        # Shared corpus (FR-25 revision): amenities belong to the project, so
        # a training turn queries geo around its real context project centre
        # (the request-bound registry snapshot carries those coords). Without
        # a resolved context project there is no centre to search around —
        # complete empty rather than a default project (fail-closed parity
        # with the rag/sql legs; the step still runs so the merge join stays
        # intact).
        project_key = await self._store_get(ctx, "project_key")
        training_context = await self._store_get(ctx, "training_context_project_key")
        if is_training_scope(project_key) and not training_context:
            await ctx.store.set("geo_result", GeoResult([], degraded=False))
            return GeoDoneEv()
        # A training turn's centre is its RESOLVED context project: the
        # request-bound snapshot (registry_key = training_retrieval_scope in
        # both facades) already carries those coords, so the snapshot branch
        # below reads the right centre. The sync branch must use the resolved
        # key too — never the '_training' marker, which project_geo_center
        # maps to the default centre.
        geo_scope = training_retrieval_scope(project_key, training_context)
        # Geo center is per-project (story 8.2/10.2): the per-request registry
        # record wins; the Settings defaults remain the legacy Camellia fallback
        # when the record carries no coordinates. Direct workflow runs outside
        # the request-bound snapshot (eval/tests) keep the legacy sync read.
        record = request_project_snapshot()
        if record is not None:
            center_lat, center_lng = record.geo_center or (
                get_cfg("geo_center_lat", 16.1052),
                get_cfg("geo_center_lng", 108.2558),
            )
        else:
            center_lat, center_lng = project_geo_center(geo_scope or "")
        try:
            result = await asyncio.wait_for(
                get_geo().places_around(
                    center_lat,
                    center_lng,
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

        # Resolve the REAL data scope once (FR-25 revision): the marker stays
        # in meta.project_key as the prompt-profile signal (generate/
        # select_answer_tier key off it), but EVERY data consumer below —
        # merge_context placeholder hydration, image enrichment, and the
        # estimates fallback via meta.retrieval_project_key — must read the
        # resolved context project, never '_training' (which matches no facts,
        # images, or estimates and silently strips training answers of their
        # structured data). Customer mode resolves byte-identically.
        session_id = await ctx.store.get("session_id")
        project_key = await self._store_get(ctx, "project_key")
        device_id = await self._store_get(ctx, "device_id")
        training = is_training_scope(project_key)
        retrieval_scope = training_retrieval_scope(
            project_key, await self._store_get(ctx, "training_context_project_key")
        )

        merged: Merged = await merge_context(
            guard.clean,
            chunks,
            sql_result.rows,
            as_of,
            retrieval_scope,
        )

        # Illustrative image enrichment is best-effort: search_images never raises,
        # so a degraded/empty result only omits images, never the pipeline.
        # Project scoping (story 10.4 / M6): the project predicate rides in the
        # search SQL itself; the unscoped legacy call is kept for project-less
        # runs (eval, direct workflow tests) whose search seam is single-arg.
        # Full price-board intent: the user asked for EVERY unit type's board, so
        # a deterministic fetch replaces semantic top_k=4 (which truncates the
        # multi-page set exactly when completeness matters).
        # Shared corpus (FR-25 revision): project imagery IS project data, so a
        # training turn gets the same scoped enrichment as a customer turn;
        # only the sales persona/CTA directive stays suppressed for the lookup
        # profile, and an unscoped training turn reads no images at all
        # (project isolation is absolute — `not retrieval_scope` also fails
        # closed on an empty-string context, never falling to the unscoped
        # legacy single-arg call).
        if training and not retrieval_scope:
            images = []
        elif retrieval_scope and is_full_price_board_query(routed.rewritten):
            images = await fetch_price_board_images(retrieval_scope)
        elif retrieval_scope:
            images = await search_images(routed.rewritten, project_key=retrieval_scope)
        else:
            images = await search_images(routed.rewritten)
        await ctx.store.set("images", images)
        conv_dir = None
        conv_state_str = None
        if not training and session_id:
            ctx_conv = get_context(session_id, device_id)
            conv_dir = conv_directive(ctx_conv.state, project_key)
            conv_state_str = ctx_conv.state
        merged.meta.update(
            query=guard.clean,
            rewritten=routed.rewritten,
            as_of=as_of.isoformat() if as_of else None,
            project_key=project_key,  # story 10.2: prompt render scope (marker in training)
            # FR-25 revision: the data scope for every downstream data
            # consumer (estimates fallback, audits) — the REAL project in
            # training mode, None only when training is unresolved (which
            # gates the fallback closed too). Customer mode: same value.
            retrieval_project_key=retrieval_scope,
            degraded=await ctx.store.get("degraded"),
            sql_row_count=len(sql_result.rows),
            has_approx=any(
                e.get("quality") in ("range", "approx") or e.get("trust_level") == "estimate"
                for e in sql_result.rows
            ),
            strong_chunks=sum(1 for c in chunks if float(c.get("score", 0.0)) >= 0.8),
            conversation_directive=conv_dir,
            conv_state=conv_state_str,  # story 4.6: answer tier selector signal
            geo_places=len((await ctx.store.get("geo_result")).places)
            if routed.routing.get("needs_geo", False)
            else 0,
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
        await self._emit(SSE_EVENT_IMAGES, {"images": images})
        return MergedEv()

    @step()
    async def generate(self, ctx: Context, ev: MergedEv) -> GeneratedEv:
        merged: Merged = await ctx.store.get("merged")
        routed: RoutedResult = await ctx.store.get("routed")
        # No training-specific grounding gate any more (FR-25 revision): a
        # training turn grounds on the shared project corpus and degrades to
        # the same ungrounded handling as customer mode.
        parts: list[str] = []
        async for token in stream_answer(
            merged, await ctx.store.get("history"), routed.high_stakes
        ):
            token = sanitize_output(token)
            parts.append(token)
            await self._emit(SSE_EVENT_TOKEN, {"text": token})
        answer = normalize_answer_display("".join(parts))
        await ctx.store.set("answer", answer)

        audit = await ctx.store.get("audit")
        audit.update(
            model=merged.meta.get("model"),
            answer_tier=merged.meta.get("answer_tier"),  # story 4.6 §7.4: audit tier
            conv_state=merged.meta.get("conv_state"),
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
                "images": await ctx.store.get("images") or [],
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
        project_key: str | None = None,
        device_id: str | None = None,
        on_event: EventCallback | None = None,
        training_context_project_key: str | None = None,
    ) -> dict:
        # One async registry read per request (B2/M1): the record is bound into
        # the request snapshot so every downstream legacy helper (identity,
        # geo, media) reads it without touching the DB synchronously. A
        # training turn reads the REAL context project (shared corpus), never
        # the mode marker.
        from api.application.services.project_config import (  # noqa: PLC0415
            bound_request_project,
            load_project_registry_record,
        )

        registry_key = training_retrieval_scope(project_key, training_context_project_key)
        record = await load_project_registry_record(registry_key)
        with bound_request_project(record):
            wf = RagQueryWorkflow(on_event=on_event if on_event is not None else self._on_event)
            handler = wf.run(
                query=query,
                session_id=session_id,
                as_of=as_of,
                history=history or [],
                project_key=project_key,
                device_id=device_id,
                training_context_project_key=training_context_project_key,
            )
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
