"""Workflow wiring test for Story 3.2: the sql_leg step stamps the
affordability structured_path into the spec before calling run_sql_leg.

The stamp is what lets the sql_leg dispatch route to run_affordability (the
v_unit_estimates leg runs before validate_spec). No LLM/DB — run_sql_leg is
monkeypatched and Context.store is seeded directly.
"""

import asyncio

from llama_index.core.workflow import Context

from api.rewrite import RoutedResult
from api.sql_leg import SqlLegResult
from api.workflow import RagQueryWorkflow, SqlRequestEv


class _Guard:
    clean = "4 tỷ mua nhà nào"


def _routed(path, spec):
    return RoutedResult(
        rewritten="4 tỷ mua nhà nào",
        routing={"needs_rag": True, "needs_sql": True, "structured_path": path},
        sql_spec=spec,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=False,
        as_of=None,
    )


def test_sql_leg_stamps_affordability_path(monkeypatch):
    from api import workflow as workflow_module

    spec_in = {
        "subject_type": "unit",
        "source": "v_unit_estimates",
        "budget_vnd": 4_000_000_000,
        "limit": 20,
    }
    captured = {}

    async def fake_run_sql_leg(spec, as_of, query):
        captured["spec"] = spec
        return SqlLegResult([], {"mode": "affordability"}, degraded=False)

    monkeypatch.setattr(workflow_module, "run_sql_leg", fake_run_sql_leg)

    async def go():
        wf = RagQueryWorkflow()
        ctx = Context(workflow=wf)
        await ctx.store.set("routed", _routed("affordability", spec_in))
        await ctx.store.set("guard", _Guard())
        await ctx.store.set("as_of_date", None)
        await ctx.store.set("degraded", [])

        done = await wf.sql_leg(ctx, SqlRequestEv())
        assert done is not None
        assert captured["spec"]["structured_path"] == "affordability"
        # The original spec body is preserved — only the path is stamped.
        assert captured["spec"]["source"] == "v_unit_estimates"
        assert captured["spec"]["budget_vnd"] == 4_000_000_000

        stored = await ctx.store.get("sql_result")
        assert stored.meta["mode"] == "affordability"
        assert stored.degraded is False

    asyncio.run(go())


def test_sql_leg_skips_sql_when_not_needed(monkeypatch):
    from api import workflow as workflow_module

    called = []

    async def fake_run_sql_leg(spec, as_of, query):
        called.append(spec)
        return SqlLegResult([], {"mode": "none"}, degraded=False)

    monkeypatch.setattr(workflow_module, "run_sql_leg", fake_run_sql_leg)

    async def go():
        wf = RagQueryWorkflow()
        ctx = Context(workflow=wf)
        routed = _routed("affordability", None)
        routed.routing = {"needs_rag": True, "needs_sql": False, "structured_path": "affordability"}
        await ctx.store.set("routed", routed)
        await ctx.store.set("guard", _Guard())
        await ctx.store.set("as_of_date", None)

        done = await wf.sql_leg(ctx, SqlRequestEv())
        assert done is not None
        assert called == []  # needs_sql False -> no run, mode 'none' result
        stored = await ctx.store.get("sql_result")
        assert stored.meta["mode"] == "none"

    asyncio.run(go())
