"""Routing tests for Story 3.2: the affordability structured_path and its forced
injection in _normalize_routed (LLM proposes, deterministic detector confirms).

Covers: allowlist extension, bare-price and declared-budget forcing (FIX-3 via
extract_price_intent as the production consumer), budget-only v_unit_offers spec
upgrade, and the no-force guardrails (compound specs, high-stakes, nl2sql,
non-price numbers). No LLM/DB — _normalize_routed is pure.
"""

from api.rewrite import _normalize_routed


def _route(data, query="4 tỷ mua nhà nào", as_of="2026-08-16"):
    return _normalize_routed(data, query, as_of)

def _budget_spec(budget_vnd):
    return {
        "subject_type": "unit",
        "source": "v_unit_offers",
        "filters": [{"field": "required_down_payment_vnd", "op": "<=", "value": budget_vnd}],
        "order_by": {"field": "required_down_payment_vnd", "dir": "asc"},
        "limit": 10,
    }

def _facts_spec():
    return {
        "subject_type": "unit",
        "source": "facts",
        "filters": [
            {"field": "area_m2", "op": ">=", "value": 60},
            {"field": "price_vnd", "op": "<=", "value": 4_000_000_000},
        ],
        "limit": 10,
    }

# allowlist accepts 'affordability'
def test_allowlist_accepts_affordability_path():
    routed = _route({"routing": {"structured_path": "affordability", "needs_sql": True}})
    assert routed.routing["structured_path"] == "affordability"

def test_allowlist_still_defaults_unknown_to_none():
    # Non-price query so the affordability detector cannot mask the fallback.
    routed = _route(
        {"routing": {"structured_path": "weird", "needs_sql": False}},
        query="căn hộ nào đang mở bán",
    )
    assert routed.routing["structured_path"] == "none"

# forced injection: bare price (extract_price_intent — production consumer, FIX-3)
def test_bare_price_forces_affordability():
    # '4 tỷ mua nhà nào' — no 'có', no LLM spec: extract_price_intent must fire.
    routed = _route({"routing": {"needs_sql": False, "structured_path": "none"}})
    assert routed.routing["structured_path"] == "affordability"
    assert routed.routing["needs_sql"] is True
    assert routed.sql_spec["source"] == "v_unit_estimates"
    assert routed.sql_spec["budget_vnd"] == 4_000_000_000
    assert routed.sql_spec["limit"] == 20
    assert "budget_injected" in routed.degraded

def test_declared_budget_forces_affordability():
    # extract_budget path: 'có 2 tỉ' -> 2B spec.
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="tôi có 2 tỉ mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["budget_vnd"] == 2_000_000_000

# budget-only v_unit_offers spec upgrade vs compound specs (never hijack)
def test_budget_only_v_unit_offers_spec_upgraded():
    # Fewshot Example-1 shape: {v_unit_offers, required_down_payment_vnd<=B}.
    routed = _route(
        {
            "routing": {"needs_sql": True, "structured_path": "spec"},
            "sql_spec": _budget_spec(2_000_000_000),
        },
        query="tôi có 2 tỉ mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["source"] == "v_unit_estimates"
    assert routed.sql_spec["budget_vnd"] == 2_000_000_000
    assert "budget_injected" in routed.degraded

def test_compound_facts_spec_not_hijacked():
    # area + price is a real compound query — stay on the LLM spec path.
    routed = _route(
        {
            "routing": {"needs_sql": True, "structured_path": "spec"},
            "sql_spec": _facts_spec(),
        },
        query="căn 60m2 giá dưới 4 tỷ",
    )
    assert routed.routing["structured_path"] == "spec"
    assert routed.sql_spec["source"] == "facts"
    assert "budget_injected" not in routed.degraded

# no-force guardrails
def test_high_stakes_no_force():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}, "high_stakes": True},
        query="thuế chuyển nhượng 4 tỷ là bao nhiêu",
    )
    assert routed.routing["structured_path"] == "none"
    assert routed.sql_spec is None
    assert "budget_injected" not in routed.degraded

def test_nl2sql_no_force():
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "nl2sql"}},
        query="trung bình giá căn hộ là bao nhiêu",
    )
    assert routed.routing["structured_path"] == "nl2sql"
    assert "budget_injected" not in routed.degraded

def test_non_price_numbers_no_force():
    # '2 ngủ'/'tầng 10' are counts/floors, not prices (FIX-3 1M floor).
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="căn 2 ngủ giá bao nhiêu",
    )
    assert routed.routing["structured_path"] == "none"
    assert routed.sql_spec is None

    routed2 = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}}, query="tầng 10 còn căn không"
    )
    assert routed2.routing["structured_path"] == "none"

def test_empty_query_no_force():
    routed = _route({"routing": {"needs_sql": False, "structured_path": "none"}}, query="")
    assert routed.routing["structured_path"] == "none"
    assert routed.sql_spec is None

# review hardening: detector guardrails + fix list F1-F5

# F1: aggregate intent must never be overridden by the affordability force
# (EH-1/BH-2/ACC-2) - 'tong gia 4 ty' is a total-price question, not a budget.
def test_aggregate_price_no_force():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="tổng giá căn hộ 4 tỷ là bao nhiêu",
    )
    assert routed.routing["structured_path"] == "none"
    assert "budget_injected" not in routed.degraded

# F2: _is_budget_only_spec must reject inverted ops, multi-filter specs, and
# malformed filters without crashing (EH-2/EH-3/BH-4/BH-5).
def test_budget_only_spec_reversed_op_not_upgraded():
    spec = _budget_spec(2_000_000_000)
    spec["filters"] = [{"field": "required_down_payment_vnd", "op": ">=", "value": 2_000_000_000}]
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "spec"}, "sql_spec": spec},
        query="tôi có 2 tỉ mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "spec"  # '>=' is inverted, not budget-only
    assert routed.sql_spec["source"] == "v_unit_offers"
    assert "budget_injected" not in routed.degraded

def test_budget_only_spec_multi_filter_not_upgraded():
    spec = _budget_spec(2_000_000_000)
    spec["filters"] = [
        {"field": "required_down_payment_vnd", "op": "<=", "value": 2_000_000_000},
        {"field": "area_m2", "op": ">=", "value": 60},
    ]
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "spec"}, "sql_spec": spec},
        query="tôi có 2 tỉ mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "spec"  # compound spec, never hijacked
    assert "budget_injected" not in routed.degraded

def test_budget_only_spec_non_dict_filters_no_crash():
    routed = _route(
        {
            "routing": {"needs_sql": True, "structured_path": "spec"},
            "sql_spec": {"source": "v_unit_offers", "filters": ["junk"]},
        },
        query="tôi có 2 tỉ mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "spec"  # malformed filters are not budget-only
    assert "budget_injected" not in routed.degraded

# F3: relational/compare price phrasings are NOT a budget - the force must not
# fire for 'trên X' (above), 'từ X đến Y' (range), 'X và Y' (compare) (BH-1).
def test_relational_above_price_not_forced():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="căn hộ nào trên 3 tỷ",
    )
    assert routed.routing["structured_path"] == "none"
    assert "budget_injected" not in routed.degraded

def test_relational_range_price_not_forced():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="căn hộ giá từ 2 tỷ đến 3 tỷ",
    )
    assert routed.routing["structured_path"] == "none"
    assert "budget_injected" not in routed.degraded

def test_compare_two_prices_not_forced():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="5 tỷ và 3 tỷ căn nào tốt hơn",
    )
    assert routed.routing["structured_path"] == "none"
    assert "budget_injected" not in routed.degraded

# 'dưới X' / 'đến X' stay budget-consistent (below-X is 'affordable up to X').
def test_below_price_still_forced():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="căn hộ nào dưới 3 tỷ",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["budget_vnd"] == 3_000_000_000

def test_ceiling_price_still_forced():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="mua nhà đến 4 tỷ",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["budget_vnd"] == 4_000_000_000

# F5: compound budget must use the FULL amount - extract_budget alone loses the
# follow-up ('2 tỉ 500 triệu' -> 2.5B, probe-confirmed) (BH-6).
def test_compound_budget_uses_full_amount():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="tôi có 2 tỉ 500 triệu mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["budget_vnd"] == 2_500_000_000

# F6: LLM-declared 'affordability' without detector confirmation must demote
# (VG-4/BH-3): no budget + no spec -> none; compound spec -> spec.
def test_llm_affordability_without_budget_demoted_to_none():
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "affordability"}},
        query="mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "none"
    assert routed.routing["needs_sql"] is False
    assert "affordability_unconfirmed" in routed.degraded

def test_llm_affordability_compound_spec_demoted_to_spec():
    routed = _route(
        {
            "routing": {"needs_sql": True, "structured_path": "affordability"},
            "sql_spec": _facts_spec(),
        },
        query="căn 60m2 giá dưới 4 tỷ",
    )
    assert routed.routing["structured_path"] == "spec"  # compound spec is honored
    assert routed.sql_spec["source"] == "facts"
    assert "affordability_unconfirmed" in routed.degraded

# VG-3: the 1M floor boundary is exclusive below, inclusive at exactly 1M.
def test_budget_floor_boundary_exact_1m_forces():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="tôi có 1 triệu mua được gì",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["budget_vnd"] == 1_000_000

def test_budget_floor_boundary_below_1m_not_forced():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="tôi có 999 nghìn mua được gì",
    )
    assert routed.routing["structured_path"] == "none"
    assert routed.sql_spec is None

# Documented pin (ACC-3): '4 tỷ 2 ngủ' - price + count. The count is not a
# price constraint; the deterministic leg covers the budget, RAG covers the
# bedroom filter. Force still fires on the 4 tỷ budget.
def test_price_plus_count_forced_documented():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}},
        query="4 tỷ 2 ngủ mua được nhà nào",
    )
    assert routed.routing["structured_path"] == "affordability"
    assert routed.sql_spec["budget_vnd"] == 4_000_000_000

