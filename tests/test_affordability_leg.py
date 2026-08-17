"""Tests for the Story 3.2 affordability leg (price_calc additions + sql_leg wiring).

Covers the DB row -> Offer mapper, the fe-evidence/meta formatters, and the
sql_leg run_affordability route. Price math itself stays under test_price_calc.py
(Story 3.1); this file tests the 3.2 wiring around it. No DB/network.
"""

from decimal import Decimal

from api.price_calc import (
    Offer,
    affordability_rows,
    affordability_summary,
    analyze_affordability,
    floor_price_vnd,
    offer_from_row,
)


# (a) DB row -> Offer mapper (v_unit_estimates columns; asyncpg types)
def _est_row(**over):
    """One v_unit_estimates-shaped row: NUMERIC(20,0)/NUMERIC(5,2) as Decimal,
    jsonb attrs as str (asyncpg default codec), NULLs as None."""
    row = {
        "subject_key": "unit:camellia/CH-10",
        "display_name": "Căn hộ 1PN",
        "project_key": "camellia",
        "attrs": '{"unit_type_key": "unit:camellia/1pn-khu-a"}',
        "policy_key": "chuan",
        "price_min_vnd": Decimal("1900000000"),
        "price_max_vnd": Decimal("2530000000"),
        "price_quality": "range",
        "deposit_pct": Decimal("30.00"),
        "term_months": 18,
        "interest_rate_pct": Decimal("0.0000"),
    }
    row.update(over)
    return row

def test_offer_from_row_coerces_decimal_and_jsonb_attrs():
    offer = offer_from_row(_est_row())
    assert offer.price_min_vnd == 1_900_000_000  # Decimal -> int
    assert offer.price_max_vnd == 2_530_000_000
    assert offer.deposit_pct == 30.0  # Decimal -> float
    assert offer.interest_rate_pct == 0.0
    assert offer.term_months == 18
    assert offer.subject_key == "unit:camellia/1pn-khu-a"  # M2: attrs unit_type_key wins
    assert offer.display_name == "Căn hộ 1PN"
    assert offer.policy_key == "chuan"
    assert offer.attrs == {"unit_type_key": "unit:camellia/1pn-khu-a"}  # jsonb str -> dict

def test_offer_from_row_handles_nullable_columns():
    offer = offer_from_row(
        _est_row(price_max_vnd=None, deposit_pct=None, term_months=None, interest_rate_pct=None)
    )
    assert offer.price_max_vnd == 1_900_000_000  # fall back to price_min
    assert offer.deposit_pct is None  # NULL != 0 (no loan policy, D6)
    assert offer.term_months is None
    assert offer.interest_rate_pct is None

def test_offer_from_row_attrs_variants():
    # attrs already a dict (non-asyncpg path) is kept.
    assert offer_from_row(_est_row(attrs={"a": 1})).attrs == {"a": 1}
    # Bad jsonb text degrades to {} rather than crashing.
    assert offer_from_row(_est_row(attrs="not-json")).attrs == {}
    # Missing/None attrs -> {}.
    assert offer_from_row(_est_row(attrs=None)).attrs == {}
    assert offer_from_row(_est_row(attrs={})).attrs == {}

def test_offer_from_row_quality_default_and_display_fallback():
    offer = offer_from_row(_est_row(price_quality=None, display_name=None))
    assert offer.price_quality == "range"
    assert offer.display_name == "unit:camellia/1pn-khu-a"  # falls back to subject key

# (b) fe-evidence rows + meta summary formatters
def _offer(subject_key, display_name, price_min, price_max, quality, deposit_pct=30.0):
    return Offer(
        subject_key=subject_key,
        display_name=display_name,
        policy_key="chuan",
        price_min_vnd=price_min,
        price_max_vnd=price_max,
        price_quality=quality,
        deposit_pct=deposit_pct,
        interest_rate_pct=0.0,
        term_months=18,
    )

def test_affordability_rows_cash_first_then_loan_with_shapes():
    offers = [
        _offer("type:A", "A", 1_500_000_000, 2_000_000_000, "range"),
        _offer("type:B", "B", 3_000_000_000, 4_000_000_000, "approx", deposit_pct=30.0),
    ]
    res = analyze_affordability(offers, 2_000_000_000)
    rows = affordability_rows(res, 0.003)

    assert [f["fe_id"] for f in rows] == ["fe-001", "fe-002"]  # sequential, cash first

    cash = rows[0]
    assert cash["fields"]["leg"] == "cash"
    assert cash["fields"]["price_min_vnd"] == 1_500_000_000
    idx = cash["fields"]["max_affordable_floor_index"]
    assert (
        cash["fields"]["highest_affordable_price_vnd"]
        == floor_price_vnd(1_500_000_000, idx, 0.003)
    )
    assert cash["subject"] == "A"
    assert cash["policy_key"] == "chuan"
    assert cash["quality"] == "range"
    assert cash["trust_level"] == "estimate"

    loan = rows[1]
    assert loan["fields"]["leg"] == "loan"
    assert loan["fields"]["required_down_payment_vnd"] == 900_000_000  # CEIL(3B * 30%)
    assert loan["quality"] == "approx"  # merge caps confidence via has_approx (D6)

def test_affordability_rows_empty_result():
    res = analyze_affordability([], 2_000_000_000)
    assert affordability_rows(res) == []

def test_affordability_summary_shape():
    offers = [
        _offer("type:A", "A", 1_500_000_000, 2_000_000_000, "range"),
        _offer("type:B", "B", 3_000_000_000, 4_000_000_000, "approx", deposit_pct=50.0),
    ]
    res = analyze_affordability(offers, 2_000_000_000)
    summary = affordability_summary(res)
    assert summary == {
        "mode": "affordability",
        "budget_vnd": 2_000_000_000,
        "lowest_price_vnd": 1_500_000_000,
        "cash_count": 1,
        "loan_count": 1,
        "has_approx": True,
    }

def test_affordability_summary_empty():
    summary = affordability_summary(analyze_affordability([], 2_000_000_000))
    assert summary["cash_count"] == 0
    assert summary["loan_count"] == 0
    assert summary["lowest_price_vnd"] is None
    assert summary["has_approx"] is False

# (c) sql_leg: run_affordability route + dispatch (injectable fetch, no pool)
def _run_affordability(spec, rows=None, fetch_kwargs=None):
    import asyncio

    from api.sql_leg import run_affordability

    async def fake_fetch():
        if fetch_kwargs and fetch_kwargs.get("raise_"):
            raise RuntimeError("db down")
        return rows or []

    async def go():
        return await run_affordability(spec, None, fetch=fake_fetch)

    return asyncio.run(go())

def _budget_spec(budget):
    return {
        "structured_path": "affordability",
        "subject_type": "unit",
        "source": "v_unit_estimates",
        "budget_vnd": budget,
        "limit": 20,
    }

def test_run_affordability_happy_path():
    rows = [_est_row()]
    result = _run_affordability(_budget_spec(2_000_000_000), rows)
    assert result.degraded is False
    assert result.meta["mode"] == "affordability"
    assert result.meta["source"] == "v_unit_estimates"
    assert result.meta["budget_vnd"] == 2_000_000_000
    assert result.meta["lowest_price_vnd"] == 1_900_000_000
    assert result.meta["cash_count"] >= 1
    assert result.meta["has_approx"] is True  # 'range' bands cap confidence (D6)
    assert result.rows[0]["fe_id"] == "fe-001"
    assert result.rows[0]["fields"]["leg"] == "cash"

def test_run_affordability_invalid_budget_degraded():
    result = _run_affordability(_budget_spec(500_000), [])
    assert result.degraded is True
    assert result.meta["degraded_reason"] == "spec_invalid"
    assert result.rows == []

def test_run_affordability_budget_missing_degraded():
    import asyncio

    from api.sql_leg import run_affordability

    async def go():
        return await run_affordability({"structured_path": "affordability"}, None, fetch=lambda: [])

    result = asyncio.run(go())
    assert result.degraded is True
    assert result.meta["degraded_reason"] == "spec_invalid"

def test_run_affordability_fetch_error_degraded():
    result = _run_affordability(_budget_spec(2_000_000_000), fetch_kwargs={"raise_": True})
    assert result.degraded is True
    assert result.meta["mode"] == "affordability"
    assert result.rows == []

def test_run_sql_leg_dispatches_affordability(monkeypatch):
    import asyncio

    from api import sql_leg as sql_leg_module
    from api.sql_leg import run_sql_leg

    async def fake_fetch():
        return [_est_row()]

    monkeypatch.setattr(sql_leg_module, "_fetch_estimates", fake_fetch)

    async def go():
        return await run_sql_leg(_budget_spec(2_000_000_000), None, "4 tỷ mua nhà nào")

    result = asyncio.run(go())
    assert result.degraded is False
    assert result.meta["mode"] == "affordability"
    assert result.meta["row_count"] == len(result.rows)

# review hardening: mapper contract (F7-F9) + runner behavior (F10-F15) + F19

# F7: offer_from_row must raise on a missing price_min_vnd - NULL is not 0
# (EH-6/BH-7). The runner pre-filters so the run path skips, never fabricates.
def test_offer_from_row_missing_price_raises():
    import pytest

    with pytest.raises(ValueError):
        offer_from_row(_est_row(price_min_vnd=None))

# F8: deposit outside [0, 100] is corrupt policy -> None (no loan policy, D6)
# (EH-7). loan_match (3.1) already skips deposit None.
def test_offer_from_row_deposit_out_of_range_to_none():
    assert offer_from_row(_est_row(deposit_pct=Decimal("150.00"))).deposit_pct is None
    assert offer_from_row(_est_row(deposit_pct=Decimal("-5.00"))).deposit_pct is None

# F9: inverted band (price_max < price_min) clamps to price_min (EH-8).
def test_offer_from_row_clamps_inverted_band():
    offer = offer_from_row(_est_row(price_max_vnd=Decimal("1000000000")))
    assert offer.price_max_vnd == 1_900_000_000  # same as price_min, not 1B

# F7 runner side: NULL price rows are skipped, good rows still answer.
def test_run_affordability_skips_missing_price_rows():
    rows = [_est_row(price_min_vnd=None), _est_row()]
    result = _run_affordability(_budget_spec(2_000_000_000), rows)
    assert result.degraded is False
    assert result.meta["row_count"] == 1

# F10: offers sorted by price_min before analyze -> fe-001 is the cheapest
# (EH-10/BH-12), deterministic ids.
def test_run_affordability_sorts_by_price_min():
    cheap = _est_row(subject_key="unit:camellia/cheap", display_name="Căn rẻ",
                     price_min_vnd=Decimal("1500000000"))
    dear = _est_row(subject_key="unit:camellia/dear", display_name="Căn đắt",
                    price_min_vnd=Decimal("2500000000"))
    result = _run_affordability(_budget_spec(3_000_000_000), [dear, cheap])
    assert result.rows[0]["subject"] == "Căn rẻ"
    assert result.rows[0]["fe_id"] == "fe-001"

# F11: evidence capped at spec limit (EH-9/BH-8); row_count = capped length.
def test_run_affordability_limit_capped():
    rows = [_est_row(subject_key=f"unit:camellia/u-{i}",
                     price_min_vnd=Decimal("1500000000")) for i in range(25)]
    spec = _budget_spec(2_000_000_000)
    spec["limit"] = 5
    result = _run_affordability(spec, rows)
    assert len(result.rows) == 5
    assert result.meta["row_count"] == 5

# F12: empty fetch (no estimates at all) degrades; rows-that-match-nothing is a
# legitimate 'nothing fits budget' and stays non-degraded (BH-9/VG-3).
def test_run_affordability_empty_fetch_degraded():
    result = _run_affordability(_budget_spec(2_000_000_000), [])
    assert result.degraded is True
    assert result.meta["degraded_reason"] == "no_estimates"

def test_run_affordability_no_match_non_degraded():
    rows = [_est_row(price_min_vnd=Decimal("9000000000"))]  # 9B > 2B budget
    result = _run_affordability(_budget_spec(2_000_000_000), rows)
    assert result.degraded is False
    assert result.rows == []
    assert result.meta["cash_count"] == 0
    assert result.meta["loan_count"] == 0

# F13: meta has_approx must mirror the merge rule (workflow.py:357) - fe rows
# are trust_level='estimate', so even all-'exact' quality caps at MEDIUM (BH-10).
def test_run_affordability_meta_has_approx_merge_consistent():
    rows = [_est_row(price_quality="exact", price_min_vnd=Decimal("1500000000"))]
    result = _run_affordability(_budget_spec(2_000_000_000), rows)
    assert result.meta["has_approx"] is True  # trust_level='estimate' rules (D6)
    assert result.rows[0]["trust_level"] == "estimate"

# F14: meta carries the sql_query for audit (BH-14).
def test_run_affordability_meta_sql_query():
    result = _run_affordability(_budget_spec(2_000_000_000), [_est_row()])
    assert "FROM v_unit_estimates" in result.meta["sql_query"]

# VG-1: ESTIMATE_COLUMNS must match the view's output columns exactly.
def test_estimate_columns_match_view():
    import re
    from pathlib import Path

    from api.sql_leg import ESTIMATE_COLUMNS

    db_path = Path(__file__).parent.parent / "db" / "camellia_estimate.sql"
    sql = db_path.read_text(encoding="utf-8")
    # Greedy .* picks the OUTER SELECT (after the cur CTE), not the CTE's own.
    m = re.search(
        r"CREATE\s+OR\s+REPLACE\s+VIEW\s+v_unit_estimates.*SELECT\s+(.*?)\s+FROM\s+fact_subjects",
        sql,
        re.S,
    )
    assert m, "SELECT block not found in db/camellia_estimate.sql"
    block = m.group(1)
    cols = set(re.findall(r"AS\s+([a-z_]+)", block)) | set(
        re.findall(r"(?:^|,)\s*(?:[a-z_]+\.)?([a-z_]+)", block)
    )
    assert set(ESTIMATE_COLUMNS) <= cols

# F19 (PO verify sentence): fe rows must carry DB attrs facts - unit codes
# (mã căn), area (m²), floor rule - so 'mã căn nào, rộng bao nhiêu m2, trong
# khoảng tầng nào' is answerable from facts, not LLM (ADR-0002 D2).
def test_affordability_rows_carry_attrs_unit_codes_and_area():
    offer = Offer(
        subject_key="unit:camellia/studio",
        display_name="Căn hộ Studio",
        policy_key="som95",
        price_min_vnd=1_720_000_000,
        price_max_vnd=2_300_000_000,
        price_quality="range",
        deposit_pct=None,
        attrs={
            "units": ["CH-09", "CH-12A", "CH-16", "CH-22"],
            "area_m2": [27.8, 31.4],
            "floor_rule": "từ tầng 3A lên, +0.3-0.4%/tầng",
        },
    )
    rows = affordability_rows(analyze_affordability([offer], 2_000_000_000), 0.003)
    f = rows[0]["fields"]
    assert f["leg"] == "cash"
    assert f["unit_codes"] == ["CH-09", "CH-12A", "CH-16", "CH-22"]
    assert f["area_m2"] == [27.8, 31.4]
    assert f["floor_rule"] == "từ tầng 3A lên, +0.3-0.4%/tầng"

def test_affordability_rows_loan_also_carries_attrs_facts():
    offer = Offer(
        subject_key="unit:camellia/1p1",
        display_name="Căn hộ 1.5PN (1PN + 1)",
        policy_key="htls",
        price_min_vnd=2_970_000_000,
        price_max_vnd=4_290_000_000,
        price_quality="range",
        deposit_pct=30.0,
        attrs={"units": ["CH-06", "CH-12", "CH-17", "CH-21"], "area_m2": [47.0, 47.3]},
    )
    rows = affordability_rows(analyze_affordability([offer], 2_000_000_000), 0.003)
    f = rows[0]["fields"]
    assert f["leg"] == "loan"
    assert f["unit_codes"] == ["CH-06", "CH-12", "CH-17", "CH-21"]
    assert f["area_m2"] == [47.0, 47.3]


