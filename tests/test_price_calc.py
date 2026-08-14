"""Unit tests for `api.price_calc` pure affordability math (Story 3.1).

Covers ADR-0002 fixes FIX-2 (floor math), FIX-3 (bare-price intent), FIX-4
(over-budget only via loan leg) plus the canonical VN money parser and the
unit->type resolution (Plan-check M2). No DB/network.
"""

import math

import pytest

from api.price_calc import (
    HIGHEST_SALE_INDEX,
    Offer,
    analyze_affordability,
    cash_match,
    extract_budget,
    extract_price_intent,
    floor_price_vnd,
    loan_match,
    parse_vn_number,
    resolve_unit_type_key,
)


# ---------------------------------------------------------------------------
# VN money parser (shared canonical — also used by rewrite/guard/extract)
# ---------------------------------------------------------------------------


def test_parse_vn_number_units():
    assert parse_vn_number("2,85 tỷ") == 2_850_000_000
    assert parse_vn_number("1 tỉ") == 1_000_000_000
    assert parse_vn_number("500 triệu") == 500_000_000
    assert parse_vn_number("2.850.000.000") == 2_850_000_000
    assert parse_vn_number("25 ngàn") == 25_000


def test_parse_vn_number_plain_digits_and_invalid():
    assert parse_vn_number("1000000") == 1_000_000
    assert parse_vn_number("abc") is None
    assert parse_vn_number("") is None
    assert parse_vn_number(None) is None


# ---------------------------------------------------------------------------
# FIX-3: budget & bare-price intent
# ---------------------------------------------------------------------------


def test_extract_budget_declared():
    assert extract_budget("tôi có 2 tỉ tiền mặt") == 2_000_000_000
    assert extract_budget("em có 500 triệu") == 500_000_000


def test_extract_budget_missing():
    assert extract_budget("mua nhà nào được vậy") is None


def test_extract_price_intent_bare_price():
    # FIX-3: no "có" keyword, amount still detected.
    assert extract_price_intent("4 tỷ mua nhà nào") == 4_000_000_000
    assert extract_price_intent("mua 3 tỷ 500 được không") == 3_500_000_000


def test_extract_price_intent_ignores_non_price_numbers():
    # Below 1M VND -> not a price intent; "2 ngủ"/"tầng 10" must be ignored.
    assert extract_price_intent("căn 2 ngủ giá bao nhiêu") is None
    assert extract_price_intent("tầng 10 có còn căn không") is None
    assert extract_price_intent("2 người thì được căn nào") is None


def test_extract_price_intent_prefers_largest():
    assert extract_price_intent("tôi hỏi 5 tỷ và 3 tỷ căn nào tốt") == 5_000_000_000


def test_word_numbers_return_none_for_fallback_note():
    # "một tỷ hai" (words, no digits) is not parseable -> caller falls back to
    # RAG + a note instead of a fabricated number (plan Story 3.1 done criteria).
    assert extract_budget("tôi có một tỷ hai") is None
    assert extract_price_intent("mua một tỷ hai được không") is None


# ---------------------------------------------------------------------------
# FIX-2: floor math — floor 25 (index 22) must hit +7%/+10%, not +6.3%/+8.4%
# ---------------------------------------------------------------------------


def test_floor_price_top_floor_exact_bands():
    base = 1_000_000_000
    # 0.4%/floor -> cumulative 10% at top (25/21 scaling).
    assert floor_price_vnd(base, HIGHEST_SALE_INDEX, 0.004) == 1_100_000_000
    # 0.28%/floor -> cumulative 7% at top.
    assert floor_price_vnd(base, HIGHEST_SALE_INDEX, 0.0028) == base + 70_000_000


def test_floor_price_lowest_floor_unchanged():
    # index 1 (floor 3A) -> no scaling.
    assert floor_price_vnd(1_000_000_000, 1, 0.004) == 1_000_000_000


def test_floor_price_monotonic():
    vals = [floor_price_vnd(1_000_000_000, i, 0.004) for i in range(1, HIGHEST_SALE_INDEX + 1)]
    assert vals == sorted(vals)


# ---------------------------------------------------------------------------
# FIX-4: cash leg vs loan leg disjointness and over-budget gating
# ---------------------------------------------------------------------------


def _offer(
    subject_key="unit:camellia/studio",
    price_min=1_900_000_000,
    price_quality="range",
    deposit_pct=30.0,
    attrs=None,
) -> Offer:
    return Offer(
        subject_key=subject_key,
        display_name="Studio",
        policy_key="chuan",
        price_min_vnd=price_min,
        price_max_vnd=2_530_000_000,
        price_quality=price_quality,
        deposit_pct=deposit_pct,
        interest_rate_pct=0.0,
        term_months=18,
        attrs=attrs or {},
    )


def test_cash_match_affordable_and_floor():
    offers = [_offer()]  # min 1.9B
    cash = cash_match(offers, 2_000_000_000, 0.003)
    assert len(cash) == 1
    offer, idx = cash[0]
    assert offer.subject_key == "unit:camellia/studio"
    # Budget 2B vs 1.9B at 0.3%/floor -> highest reachable floor.
    assert 1 <= idx < HIGHEST_SALE_INDEX


def test_cash_match_budget_exactly_price_min():
    offers = [_offer(price_min=2_000_000_000)]
    cash = cash_match(offers, 2_000_000_000, 0.003)
    assert len(cash) == 1  # equal budget counts as affordable


def test_cash_match_below_lowest_floor():
    offers = [_offer(price_min=1_900_000_000)]
    assert cash_match(offers, 1_000_000_000, 0.003) == []


def test_loan_match_shows_over_budget_only_when_deposit_fits():
    offers = [_offer(price_min=3_000_000_000, deposit_pct=30.0)]
    # Deposit 900M <= budget 1_000_000_000 -> loan leg eligible.
    loan = loan_match(offers, 1_000_000_000)
    assert len(loan) == 1


def test_loan_match_hides_over_budget_when_deposit_too_high():
    offers = [_offer(price_min=3_000_000_000, deposit_pct=50.0)]
    # Deposit 1.5B > budget 1_000_000_000 -> NOT shown.
    assert loan_match(offers, 1_000_000_000) == []


def test_loan_match_requires_exact_deposit_pct():
    # deposit_pct None = no loan policy (NULL != 0%) — never in loan leg.
    offers = [_offer(price_min=3_000_000_000, deposit_pct=None)]
    assert loan_match(offers, 1_000_000_000) == []


def test_loan_match_excludes_cash_affordable():
    # price_min <= budget -> cash material only; loan leg stays disjoint.
    offers = [_offer(price_min=900_000_000, deposit_pct=30.0)]
    assert loan_match(offers, 1_000_000_000) == []


def test_analyze_affordability_partitions_and_approx():
    cash_offer = _offer(price_min=1_900_000_000, price_quality="range")
    loan_offer = _offer(
        subject_key="unit:camellia/1p1", price_min=3_000_000_000, price_quality="approx"
    )
    res = analyze_affordability([cash_offer, loan_offer], 2_000_000_000)
    assert res["budget_vnd"] == 2_000_000_000
    assert res["lowest_price_vnd"] == 1_900_000_000
    assert len(res["cash"]) == 1
    assert len(res["loan"]) == 1
    assert res["has_approx"] is True

    res2 = analyze_affordability([cash_offer], 2_000_000_000)
    assert res2["has_approx"] is True  # range bands cap confidence at MEDIUM (D6)
    assert res2["lowest_price_vnd"] == 1_900_000_000


def test_analyze_affordability_empty():
    res = analyze_affordability([], 2_000_000_000)
    assert res["cash"] == []
    assert res["loan"] == []
    assert res["lowest_price_vnd"] is None
    assert res["has_approx"] is False


# ---------------------------------------------------------------------------
# Plan-check M2: concrete unit -> type band resolution
# ---------------------------------------------------------------------------


def test_resolve_unit_type_key_from_attrs():
    attrs = {"unit_type_key": "unit:camellia/2pn-noi-khu"}
    assert resolve_unit_type_key(attrs, "unit:camellia/CH-10") == "unit:camellia/2pn-noi-khu"


def test_resolve_unit_type_key_fallback_for_type_row():
    # Type rows carry no unit_type_key -> fall back to their own key.
    assert resolve_unit_type_key({}, "unit:camellia/2pn-goc") == "unit:camellia/2pn-goc"
    assert resolve_unit_type_key(None, "unit:camellia/3pn") == "unit:camellia/3pn"