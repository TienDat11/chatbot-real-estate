"""Pure affordability math (ADR-0002 D2) — no I/O, fully unit-testable.

Canonical Vietnamese money parser lives here so rewrite/guard/extract reuse one
implementation (SOLID, no per-feature copies). Floor math per FIX-2; loan-leg
gating per FIX-4; bare-price intent per FIX-3; unit->type resolution per
Plan-check M2.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

# Floor-math scale (FIX-2): cumulative_pct = pct*(idx-1)*25/21. Sale floors are
# 3A..25 -> 22 indices; 25 normalizes pct so the top (+7%/+10%) matches the
# offer sheet exactly and avoids the naive +6.3%/+8.4% undercount.
FLOOR_NORM = 25 / 21
HIGHEST_SALE_INDEX = 22

# Vietnamese money units -> VND multipliers (shared data for every parser).
VN_UNITS = {
    "tỷ": 1_000_000_000,
    "tỉ": 1_000_000_000,
    "triệu": 1_000_000,
    "ngàn": 1_000,
    "nghìn": 1_000,
}

_NUMBER_RE = re.compile(r"(\d[\d.,]*)\s*(tỷ|tỉ|triệu|ngàn|nghìn)?")
_BUDGET_RE = re.compile(
    r"có\s+([\d.,]+)\s*(tỷ|tỉ|triệu|ngàn|nghìn)?\s*(?:tiền|đồng|vnd)?",
    re.IGNORECASE,
)


def parse_vn_number(text: str) -> int | None:
    """Parse a Vietnamese money literal: '2,85 tỷ' -> 2_850_000_000; plain digits -> int.

    Conventions: comma is the decimal separator, dot the thousand separator.
    """
    t = (text or "").strip().lower().replace(" ", " ")
    m = _NUMBER_RE.search(t)
    if not m:
        return None
    num_part, unit = m.group(1), (m.group(2) or "")
    if "," in num_part and "." in num_part:
        num_part = num_part.replace(".", "").replace(",", ".")  # 2.850.000,50
    elif "," in num_part:
        num_part = num_part.replace(",", ".")  # 2,85 -> 2.85
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", num_part):
        num_part = num_part.replace(".", "")  # 2.500.000.000 -> 2500000000
    elif re.fullmatch(r"\d{1,2}\.\d+", num_part):
        pass  # Western decimal ('3.5' -> 3.5): keep as-is for float() below.
    elif "." in num_part:
        return None  # malformed dot grouping ('1234.567') - reject, not fabricated.
    try:
        value = float(num_part)
    except ValueError:
        return None
    value *= VN_UNITS.get(unit, 1)
    return int(round(value))


def extract_budget(query: str) -> int | None:
    """Declared budget ('tôi có 2 tỉ...') converted to VND, or None."""
    m = _BUDGET_RE.search((query or "").lower())
    if not m:
        return None
    raw = f"{m.group(1)} {m.group(2) or ''}".strip()
    return parse_vn_number(raw)


def extract_price_intent(query: str) -> int | None:
    """Price-intent amount even without 'có': '4 tỷ mua nhà nào' -> 4_000_000_000.

    Returns the largest money literal >= 1M VND in the query, else None. The
    1M floor keeps non-price numbers ('có 2 ngủ', 'tầng 10') out (FIX-3).
    Handles "X tỷ Y" shorthand (3 tỷ 500 = 3 tỷ 500 triệu) and the explicit
    "X tỷ Y triệu" form by binding the follow-up literal to the tỷ amount.
    """
    matches = list(_NUMBER_RE.finditer((query or "").lower()))
    candidates = []
    i = 0
    while i < len(matches):
        m = matches[i]
        amount = parse_vn_number(m.group(0))
        if amount is not None:
            prev_unit = m.group(2)
            # "3 tỷ 500" -> 3,5 tỷ: merges a unitless <1k follow-up as triệu,
            # or an explicit "Y triệu" that would otherwise be outvoted by the
            # tỷ literal (4 tỷ + 500 triệu = 4,5 tỷ, not just 4 tỷ).
            if prev_unit in ("tỷ", "tỉ") and i + 1 < len(matches):
                nxt = matches[i + 1]
                if nxt.start() - m.end() <= 4:
                    extra = parse_vn_number(nxt.group(0))
                    # Unitless follow-up must be a 100..999 money shorthand
                    # ('3 tỷ 500' = 3,5 tỷ); counts like '4 tỷ 2 ngủ' are < 100
                    # and must not fabricate a price (FIX-3).
                    if extra is not None and nxt.group(2) is None and 100 <= extra < 1_000:
                        amount += extra * 1_000_000
                        i += 1
                    elif extra is not None and nxt.group(2) == "triệu" and extra < 1_000_000_000:
                        amount += extra
                        i += 1
            if amount >= 1_000_000:
                candidates.append(amount)
        i += 1
    return max(candidates) if candidates else None


def floor_price_vnd(base_vnd: int, floor_index: int, scenario_pct: float = 0.003) -> int:
    """Band price at a sale floor (1-based from 3A) under the FIX-2 formula."""
    cumulative = scenario_pct * (floor_index - 1) * FLOOR_NORM
    return int(round(base_vnd * (1 + cumulative)))


@dataclass(frozen=True)
class Offer:
    """One v_unit_estimates row: price band + optional loan policy per method."""

    subject_key: str
    display_name: str
    policy_key: str
    price_min_vnd: int
    price_max_vnd: int
    price_quality: str = "range"  # 'range'/'approx' — 'approx' caps confidence
    deposit_pct: float | None = None  # None = no loan policy (NULL != 0, D6)
    interest_rate_pct: float | None = None
    term_months: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


def resolve_unit_type_key(attrs: dict[str, Any] | None, fallback_key: str) -> str:
    """Type-level subject_key a concrete unit belongs to (Plan-check M2).

    CH-10/CH-11 carry attrs.unit_type_key pointing to their band; type rows
    (no unit_type_key) fall back to their own subject_key.
    """
    if attrs and attrs.get("unit_type_key"):
        return str(attrs["unit_type_key"])
    return fallback_key


def cash_match(
    offers: list[Offer], budget_vnd: int, scenario_pct: float = 0.003
) -> list[tuple[Offer, int]]:
    """Offers affordable on price_min (<= budget) + the highest reachable sale floor.

    Returns (offer, max_affordable_floor_index); floor_index is clamped to the
    3A..25 band so it never exceeds the highest sale floor regardless of budget.
    """
    out: list[tuple[Offer, int]] = []
    for o in offers:
        if o.price_min_vnd > budget_vnd:
            continue
        idx = HIGHEST_SALE_INDEX
        while idx > 1 and floor_price_vnd(o.price_min_vnd, idx, scenario_pct) > budget_vnd:
            idx -= 1
        out.append((o, idx))
    return out


def loan_match(offers: list[Offer], budget_vnd: int) -> list[Offer]:
    """Over-budget offers affordable via deposit only (FIX-4).

    Shows an offer strictly above budget only when CEIL(price_min*deposit%) is
    within budget; deposit_pct None (no loan policy) never participates.
    """
    out: list[Offer] = []
    for o in offers:
        if o.deposit_pct is None:
            continue
        if o.price_min_vnd <= budget_vnd:
            continue  # already cash-affordable; keep the legs disjoint
        required = math.ceil(o.price_min_vnd * o.deposit_pct / 100)
        if required <= budget_vnd:
            out.append(o)
    return out


def analyze_affordability(
    offers: list[Offer], budget_vnd: int, scenario_pct: float = 0.003
) -> dict[str, Any]:
    """Partition offers into cash + loan legs at a budget. Pure — caller formats.

    Keys: budget_vnd, lowest_price_vnd (across all offers/policies), cash
    (list of (offer, max_floor_index)), loan (list of Offer), has_approx
    (any affordable offer is quality 'range'/'approx' -> confidence cap MEDIUM, D6).
    """
    cash = cash_match(offers, budget_vnd, scenario_pct)
    loan = loan_match(offers, budget_vnd)
    has_approx = any(o.price_quality in ("range", "approx") for o, _ in cash) or any(
        o.price_quality in ("range", "approx") for o in loan
    )
    prices = [o.price_min_vnd for o in offers if o.price_min_vnd]
    return {
        "budget_vnd": budget_vnd,
        "lowest_price_vnd": min(prices) if prices else None,
        "cash": cash,
        "loan": loan,
        "has_approx": has_approx,
    }


def offer_from_row(row: dict) -> Offer:
    """Map one v_unit_estimates row to an Offer; asyncpg types -> plain values.

    NUMERIC columns arrive as Decimal, jsonb attrs as str: coerce both here so
    the rest of the module stays pure ints/floats/dicts. NULL deposit_pct stays
    None (no loan policy, D6); M2 resolves concrete units to their type band.
    """
    raw_attrs = row.get("attrs")
    if isinstance(raw_attrs, str):
        try:
            attrs = json.loads(raw_attrs)
        except (ValueError, TypeError):
            attrs = {}
    elif isinstance(raw_attrs, dict):
        attrs = raw_attrs
    else:
        attrs = {}
    if not isinstance(attrs, dict):
        attrs = {}

    subject_key = resolve_unit_type_key(attrs, str(row.get("subject_key") or ""))
    price_min_raw = row.get("price_min_vnd")
    if price_min_raw is None:
        # NULL price is a data defect, never a 0-VND offer (fabrication).
        raise ValueError(f"price_min_vnd missing for {subject_key}")
    price_min = int(price_min_raw)
    price_max_raw = row.get("price_max_vnd")
    # Inverted band (max < min) clamps to min rather than emitting nonsense.
    price_max = max(int(price_max_raw), price_min) if price_max_raw is not None else price_min

    deposit_raw = row.get("deposit_pct")
    rate_raw = row.get("interest_rate_pct")
    term_raw = row.get("term_months")

    # Deposit outside [0, 100] is corrupt policy -> None (no loan policy, D6);
    # loan_match already skips deposit None, so such offers are cash-only.
    deposit_pct = float(deposit_raw) if deposit_raw is not None else None
    if deposit_pct is not None and not 0 <= deposit_pct <= 100:
        deposit_pct = None

    return Offer(
        subject_key=subject_key,
        display_name=str(row.get("display_name") or subject_key),
        policy_key=str(row.get("policy_key") or ""),
        price_min_vnd=price_min,
        price_max_vnd=price_max,
        price_quality=str(row.get("price_quality") or "range"),
        deposit_pct=deposit_pct,
        interest_rate_pct=float(rate_raw) if rate_raw is not None else None,
        term_months=int(term_raw) if term_raw is not None else None,
        attrs=attrs,
    )


def affordability_rows(result: dict, scenario_pct: float = 0.003) -> list[dict]:
    """Format analyze_affordability() as FACT_EVIDENCE blocks (one per match).

    Cash rows carry the top affordable sale floor + its band price; loan rows
    carry the required down payment (FIX-4). quality/trust_level feed the merge's
    has_approx confidence cap (D6). Ids are sequential — cash first, then loan.
    """
    fe: list[dict] = []
    n = 0
    for offer, floor_index in result["cash"]:
        n += 1
        band_price = floor_price_vnd(offer.price_min_vnd, floor_index, scenario_pct)
        fe.append(
            {
                "fe_id": f"fe-{n:03d}",
                "subject": offer.display_name or offer.subject_key,
                "policy_key": offer.policy_key,
                "fields": {
                    "leg": "cash",
                    "price_min_vnd": offer.price_min_vnd,
                    "price_max_vnd": offer.price_max_vnd,
                    "price_quality": offer.price_quality,
                    "deposit_pct": offer.deposit_pct,
                    "term_months": offer.term_months,
                    "interest_rate_pct": offer.interest_rate_pct,
                    "max_affordable_floor_index": floor_index,
                    "highest_affordable_price_vnd": band_price,
                    # attrs facts (mã căn / m² / tầng) — the PO verify sentence
                    # needs them; all three are DB facts, never LLM (ADR-0002 D2).
                    "unit_codes": (offer.attrs or {}).get("units"),
                    "area_m2": (offer.attrs or {}).get("area_m2"),
                    "floor_rule": (offer.attrs or {}).get("floor_rule"),
                },
                "note": (
                    f"mua được đến tầng {floor_index} (giá {band_price:,} VND), "
                    f"giá gốc từ {offer.price_min_vnd:,} VND"
                ),
                "quality": offer.price_quality,
                "trust_level": "estimate",
            }
        )
    for offer in result["loan"]:
        n += 1
        down_payment = (
            math.ceil(offer.price_min_vnd * offer.deposit_pct / 100)
            if offer.deposit_pct is not None
            else None
        )
        fe.append(
            {
                "fe_id": f"fe-{n:03d}",
                "subject": offer.display_name or offer.subject_key,
                "policy_key": offer.policy_key,
                "fields": {
                    "leg": "loan",
                    "price_min_vnd": offer.price_min_vnd,
                    "price_max_vnd": offer.price_max_vnd,
                    "price_quality": offer.price_quality,
                    "deposit_pct": offer.deposit_pct,
                    "term_months": offer.term_months,
                    "interest_rate_pct": offer.interest_rate_pct,
                    "required_down_payment_vnd": down_payment,
                    "unit_codes": (offer.attrs or {}).get("units"),
                    "area_m2": (offer.attrs or {}).get("area_m2"),
                    "floor_rule": (offer.attrs or {}).get("floor_rule"),
                },
                "note": (
                    f"trả trước {down_payment:,} VND (deposit {offer.deposit_pct:g}%), "
                    f"giá gốc {offer.price_min_vnd:,} VND"
                ),
                "quality": offer.price_quality,
                "trust_level": "estimate",
            }
        )
    return fe


def affordability_summary(result: dict) -> dict:
    """Meta summary for the affordability leg (counterpart to build_fact_evidence)."""
    return {
        "mode": "affordability",
        "budget_vnd": result["budget_vnd"],
        "lowest_price_vnd": result["lowest_price_vnd"],
        "cash_count": len(result["cash"]),
        "loan_count": len(result["loan"]),
        "has_approx": result["has_approx"],
    }
