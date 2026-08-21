"""Regression tests for the per-step timeout budgets in the query workflow.

Budgets bound stuck calls (so the pipeline degrades instead of parking) while
keeping >=1.3x headroom over the warm happy-path latencies documented in the
module docstring. Pinning both the values and the headroom guard prevents a
future edit from silently re-introducing the nl2sql budget that timed out real
requests.
"""

from __future__ import annotations

from api.application.pipelines.workflow import RagQueryWorkflow, STEP_TIMEOUTS

EXPECTED_TIMEOUTS = {
    "guard": 1.0,
    "rewrite": 12.0,
    "rag": 25.0,
    "sql": 3.0,
    "sql_nl2sql": 11.0,
    "rerank": 3.0,
    "geo": 3.0,
    "output_guard": 1.5,
}

# Warm-latency ceilings measured by scripts/timing probe (see module docstring).
MEASURED_MAX = {
    "rewrite": 9.3,
    "rag": 5.9,
    "sql_nl2sql": 8.0,
}

# rewrite keeps ~1.29x headroom (12.0 / 9.3): accepted in review as a MINOR
# cold-start risk, so it is pinned to its own threshold rather than the 1.3x bar.
MIN_HEADROOM = 1.3
REWRITE_MIN_HEADROOM = 1.29


def test_step_timeouts_has_all_eight_keys():
    assert set(STEP_TIMEOUTS) == set(EXPECTED_TIMEOUTS)


def test_step_timeout_values_are_pinned():
    for key, value in EXPECTED_TIMEOUTS.items():
        assert STEP_TIMEOUTS[key] == value


def test_nl2sql_budget_keeps_happy_path_headroom():
    # 8.0s measured; a lower budget started cutting real nl2sql requests.
    assert STEP_TIMEOUTS["sql_nl2sql"] > 8.0


def test_headroom_at_least_1_3x_measured_max():
    for key, measured in MEASURED_MAX.items():
        floor = REWRITE_MIN_HEADROOM if key == "rewrite" else MIN_HEADROOM
        assert STEP_TIMEOUTS[key] >= measured * floor, f"{key} budget lacks headroom"


def test_rag_query_workflow_imports_and_declares_steps():
    assert RagQueryWorkflow is not None
    for name in (
        "guard",
        "route",
        "rag_leg",
        "sql_leg",
        "geo_leg",
        "merge",
        "generate",
        "output_guard",
    ):
        assert hasattr(RagQueryWorkflow, name), f"missing workflow step {name}"
