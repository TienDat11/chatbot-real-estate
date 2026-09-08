"""Estimates fallback leg — indicative board prices into empty FACT_EVIDENCE.

Bug B: when routing builds a v_unit_offers spec but the requested project only
publishes an indicative price board (Camellia "giá treo" per type, no per-unit
offers), the SQL leg returns zero rows, FACT_EVIDENCE stays empty, and
generation answers "chưa cập nhật bảng giá" even though the treo board exists
in both the documents registry and v_unit_estimates.

This leg reads v_unit_estimates (the same view the affordability leg uses)
scoped to the request's project and materializes fe-xxx evidence blocks, so
superlative price questions ("căn rẻ nhất", "studio vip nhất" — which never
carry a numeric budget and thus never fire the affordability path) stay
grounded, cited, and honestly labelled as estimate/range figures.

Firing gate is deliberately narrow: only when FACT_EVIDENCE is EMPTY and a
project scope exists — a data-backed answer is never rewritten here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .merge import Merged, build_evidence_context, build_facts
from .sql_leg import with_rls_identity

logger = logging.getLogger("api.estimates_fallback")

# Mirrors sql_leg.ESTIMATE_COLUMNS minus attrs (not user-facing evidence).
_ESTIMATE_VIEW = "v_unit_estimates"
# Cap applies to unit TYPES (after per-type aggregation), never to raw rows:
# row-level slicing dropped whole types whenever cheaper ones filled the limit.
_EVIDENCE_LIMIT = 8

# Deterministic price/unit-intent hint ("giá" but NOT "giá trị"): keeps legal,
# greeting, and utility turns free of irrelevant unit-price evidence.
_UNIT_PRICE_HINT_RE = re.compile(
    r"căn\s*hộ|\bcăn\b|studio|\bpn\b|bảng\s*giá|giá(?! trị)|rẻ\s*nhất|cao\s*cấp|vip",
    re.IGNORECASE,
)


def _policy_range_label(row: dict) -> str:
    """Compact "min-max" label for one (type, policy) row; single bound tolerated."""
    bounds = [
        int(row[key]) for key in ("price_min_vnd", "price_max_vnd") if row.get(key) is not None
    ]
    return "-".join(str(b) for b in bounds)


def _aggregate_type_rows(type_rows: list[dict]) -> dict[str, Any] | None:
    """Collapse one unit type's per-policy rows into aggregate price fields.

    The board publishes one range per payment policy, so the honest per-type
    figure is the min of mins / max of maxes across policies. Every policy's own
    bounds stay in ``fields`` as flat numerics so the output guard grounds ANY
    figure the answer quotes, and ``policy_price_ranges`` keeps the readable
    per-policy breakdown so generation can name the payment methods. None when
    the type carries no priced row at all.
    """
    mins = [int(r["price_min_vnd"]) for r in type_rows if r.get("price_min_vnd") is not None]
    maxs = [int(r["price_max_vnd"]) for r in type_rows if r.get("price_max_vnd") is not None]
    if not mins and not maxs:
        return None

    fields: dict[str, Any] = {}
    display_name = next((r["display_name"] for r in type_rows if r.get("display_name")), None)
    if display_name:
        fields["display_name"] = display_name
    if mins:
        fields["price_min_vnd"] = min(mins)
    if maxs:
        fields["price_max_vnd"] = max(maxs)
    ordered = sorted(type_rows, key=lambda r: str(r.get("policy_key") or ""))
    policy_price_ranges: dict[str, str] = {}
    for row in ordered:
        policy_key = str(row.get("policy_key") or "")
        for side in ("min", "max"):
            column = f"price_{side}_vnd"
            if row.get(column) is not None:
                fields[f"price_{side}_{policy_key}"] = int(row[column])
        policy_price_ranges[policy_key or "-"] = _policy_range_label(row)
    fields["policy_price_ranges"] = policy_price_ranges
    return fields


def build_estimate_evidence(rows: list[dict], limit: int = _EVIDENCE_LIMIT) -> list[dict]:
    """v_unit_estimates rows -> fe-001.. evidence blocks, ONE PER UNIT TYPE.

    Rows arrive one-per-(type, payment policy): slicing flat rows cheapest-first
    dropped whole types once cheaper ones filled the cap, so prices are first
    aggregated per subject_key and only then capped — cheapest-first by
    aggregated min, stable subject_key tie-break. Pure so tests never touch the
    pool; types without any priced row are skipped (never fabricate a 0 figure).
    """
    priced = [
        r for r in rows if r.get("price_min_vnd") is not None or r.get("price_max_vnd") is not None
    ]
    by_subject: dict[str, list[dict]] = {}
    for row in priced:
        by_subject.setdefault(str(row.get("subject_key") or "unit:unknown"), []).append(row)

    aggregated: list[tuple[int | float, str, dict[str, Any]]] = []
    for subject_key, type_rows in by_subject.items():
        fields = _aggregate_type_rows(type_rows)
        if fields is None:
            continue
        # Sort floor: a type with only upper bounds still sorts after priced ones.
        sort_floor = fields.get("price_min_vnd", float("inf"))
        aggregated.append((sort_floor, subject_key, fields))
    aggregated.sort(key=lambda item: (item[0], item[1]))

    evidence: list[dict] = []
    for i, (_, subject_key, fields) in enumerate(aggregated[:limit], start=1):
        evidence.append(
            {
                "fe_id": f"fe-{i:03d}",
                "subject": subject_key,
                # One block aggregates rows across payment policies (see
                # policy_price_ranges), so no single key applies — None keeps
                # the SQL-leg evidence contract (merge reads policy_key).
                "policy_key": None,
                "fields": fields,
                # Indicative board semantics: a range across floors/types, NOT a
                # per-unit quote — wording must say "giá treo hiện tại".
                "note": (
                    "giá treo định hướng theo phương thức thanh toán "
                    "(range toàn dự án, chưa phải giá bán chính thức từng căn)"
                ),
                "quality": "range",
                "trust_level": "estimate",
                "range": {
                    "min": fields.get("price_min_vnd"),
                    "max": fields.get("price_max_vnd"),
                },
            }
        )
    return evidence


async def fetch_unit_estimates(project_key: str) -> list[dict]:
    """Current v_unit_estimates rows scoped to one project (ro_query role)."""
    sql = (
        f"SELECT subject_key, display_name, project_key, policy_key, "
        f"price_min_vnd, price_max_vnd, deposit_pct, term_months, interest_rate_pct "
        f"FROM {_ESTIMATE_VIEW} WHERE project_key = $1"
    )
    async with with_rls_identity(timeout_s=1.5) as conn:
        recs = await conn.fetch(sql, project_key)
    return [dict(r) for r in recs]


async def apply_estimates_fallback(merged: Merged) -> bool:
    """Fill empty FACT_EVIDENCE from v_unit_estimates; True when applied.

    Mutates ``merged`` in place (the established generate-step pattern): facts,
    evidence_blocks, plus meta.sql_row_count / meta.has_approx / meta flag so
    guard_output sees deterministic provenance while has_approx=True keeps the
    confidence capped at MEDIUM — correct for indicative ranges.
    """
    if merged.facts:
        return False
    # FR-25 revision: scope the estimates read to the RESOLVED retrieval
    # project (meta.retrieval_project_key, set by the merge step) — the raw
    # meta.project_key is the '_training' marker on training turns and matches
    # no estimates row. A training turn with no resolved project reads None
    # here and gates closed below (same fail-closed rule as the retrieval
    # legs); callers that predate the retrieval key (eval, legacy merges) fall
    # back to meta.project_key unchanged.
    project_key = merged.meta.get(
        "retrieval_project_key", merged.meta.get("project_key")
    )
    if not project_key:
        return False
    query_text = " ".join(str(merged.meta.get(k) or "") for k in ("query", "rewritten"))
    if not _UNIT_PRICE_HINT_RE.search(query_text):
        return False
    try:
        rows = await fetch_unit_estimates(str(project_key))
    except Exception as exc:  # noqa: BLE001 — fallback must never break generation
        logger.warning("estimates fallback fetch failed (ignored): %s", exc)
        return False
    evidence = build_estimate_evidence(rows)
    if not evidence:
        return False
    merged.evidence_blocks = build_evidence_context(evidence)
    merged.facts = build_facts(evidence)
    merged.meta["sql_row_count"] = len(evidence)
    merged.meta["has_approx"] = True
    merged.meta["estimates_fallback"] = True
    return True


__all__ = [
    "apply_estimates_fallback",
    "build_estimate_evidence",
    "fetch_unit_estimates",
]
