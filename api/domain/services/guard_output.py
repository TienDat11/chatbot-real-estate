"""L4 output guard (plan §4.7): numeric grounding, orphan detection, citations, confidence.

Financial numbers must byte-match a normalized FACT_EVIDENCE value; orphans force LOW
and review, citation grounding is best-effort, and LOW/high-stakes set requires_review.
Plain async — testable without a framework.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger("api.guard_output")

# Numbers with Vietnamese units (tỷ/triệu/ngàn + %/m2); "financial" only when a
# unit is present or the magnitude is >= 1M (skips articles, years, floors).
_NUMBER_RE = re.compile(r"(\d[\d.,]*)\s*(tỷ|tỉ|triệu|ngàn|nghìn|đồng|đ|vnđ|%|m2|m²)?", re.IGNORECASE)
_FINANCIAL_MIN = 1_000_000

_VN_UNITS = {
    "tỷ": Decimal("1e9"),
    "triệu": Decimal("1e6"),
    "ngàn": Decimal("1e3"),
    "nghìn": Decimal("1e3"),
}

# --- Story 4.2: contextual disclosure + robot-phrase style checks (L4, non-confidence) ---
# Disclosure keyword set for price/estimate answers (prompt §DISCLOSURE THEO NGỮ CẢNH).
_PRICE_DISCLOSURE_KEYS = ("định hướng", "bảng hàng chính thức", "chưa xác nhận chính thức", "ước lượng")
# High-stakes answers must steer to a specialist (prompt §DISCLOSURE rule 3).
_HIGH_STAKES_DISCLOSURE_KEY = "chuyên viên"
# Robot phrases banned by prompt §GIỌNG VĂN — flag style_warn only (never lowers confidence).
_ROBOT_PHRASES = (
    "dựa trên thông tin được cung cấp",
    "như đã nêu ở trên",
    "theo yêu cầu của bạn",
    "tôi là ai",
    "tôi là trợ lý ảo",
    "hy vọng thông tin hữu ích",
)
# Em-dash banned in answers (prompt §GIỌNG VĂN) — style warning, not confidence.
_EM_DASH = "—"
# Price/estimate answer indicator: value words OR has_approx meta flag (set by workflow).
_PRICE_HINT_RE = re.compile("(giá|tỷ đồng|triệu đồng|đồng\\s*\\(|định hướng|m²|m2)", re.IGNORECASE)


@dataclass
class GuardResult:
    confidence: str  # HIGH | MEDIUM | LOW
    requires_review: bool
    verdicts: dict = field(default_factory=dict)


def _to_decimal(v: Any) -> Decimal | None:
    """Normalize a value to Decimal — safe for numbers beyond 2^53."""
    if isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip().replace(" ", "").replace(" ", "")
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None
    return None


def _normalize_amount_text(text: str) -> Decimal | None:
    """Normalize one numeric match: '2,85 tỷ' -> 2850000000; '25%' -> 25; '2.000.000.000' -> int."""
    t = (text or "").strip().lower()
    unit = None
    for u in _VN_UNITS:
        if u in t:
            unit = u
            t = t.replace(u, "").strip()
            break
    t = t.replace("%", "").replace("m2", "").replace("m²", "").replace("đồng", "").strip()
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    else:
        t = t.replace(".", "")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    if unit:
        d *= _VN_UNITS[unit]
    return d


def extract_amounts(text: str) -> list[Decimal]:
    """All normalized financial numbers in the answer, for the orphan check.

    Keeps figures with a money/%/area unit or magnitude >= 1M to avoid false positives
    on laws, years, floors.
    """
    out: list[Decimal] = []
    for m in _NUMBER_RE.finditer(text or ""):
        d = _normalize_amount_text(m.group(0))
        if d is None:
            continue
        unit = m.group(2)
        if unit is None and abs(d) < _FINANCIAL_MIN:
            continue
        out.append(d)
    return out


def evidence_values(facts: list[dict]) -> list[Decimal]:
    """Normalized field values from FACT_EVIDENCE blocks."""
    out: list[Decimal] = []
    for fact in facts or []:
        for v in (fact.get("fields") or {}).values():
            d = _to_decimal(v)
            if d is not None:
                out.append(d)
    return out


def _byte_match(d: Decimal, evidence: list[Decimal]) -> bool:
    """Byte-match (normalized): true when an evidence value equals d exactly."""
    return any(d == e for e in evidence)


def _citation_grounding(answer: str, sources: list[dict], known_fe_ids: list[str]) -> dict:
    """Best-effort: [fe-xxx] citations and source titles must appear in sources.

    Without chunk content here, verdict stays 'pending' when there is no span to
    check; 'pass' when every citation has a matching source.
    """
    known = set(known_fe_ids or [])
    verdict: dict[str, Any] = {"status": "pending", "detail": "no span to check"}
    fe_cites = re.findall(r"\[fe-\d{3}\]", answer or "")
    if fe_cites:
        missing = [c for c in fe_cites if c not in known]
        verdict = {"status": "pass" if not missing else "fail", "fe_citations": len(fe_cites), "missing": missing}
    # Source titles — best-effort substring check
    titles = [s.get("title") or "" for s in sources or []]
    if titles:
        matched = [t for t in titles if t and (t in answer or any(w in answer for w in _title_words(t)))]
        verdict["source_titles_matched"] = len(matched)
        verdict["source_titles_total"] = len(titles)
    return verdict


def _title_words(title: str) -> list[str]:
    """Distinctive title words (>= 3 chars) used for substring matching."""
    return [w for w in re.split(r"\s+", title) if len(w) >= 3]


def _confidence_3tier(
    numeric_pass: bool,
    sql_row_count: int,
    strong_chunks: int,
    degraded: list[str],
    has_approx: bool,
) -> str:
    """HIGH when grounding passes and SQL has >= 1 row or >= 2 chunks at rerank >= 0.8;

    MEDIUM with a single source, degraded, or approx; LOW when not passing.
    """
    if not numeric_pass:
        return "LOW"
    any_source = sql_row_count >= 1 or strong_chunks >= 1
    if not any_source:
        return "LOW"
    provenance_ok = sql_row_count >= 1 or strong_chunks >= 2
    if provenance_ok and not degraded and not has_approx:
        return "HIGH"
    return "MEDIUM"


def _contextual_disclosure_verdict(
    answer: str,
    has_approx: bool,
    high_stakes: bool,
) -> dict:
    """Story 4.2 contextual disclosure (replaces always-on AI line check).

    price/estimate answer -> must carry a disclosure keyword ("định hướng" ...);
    high-stakes (cầm cố/công chứng/thuế) -> must steer to "chuyên viên";
    normal answers -> no disclosure required (FE footer owns the always-on line).

    Returns a verdict dict; never affects confidence (L4 grounding owns that).
    """
    low = (answer or "").lower()
    is_price = has_approx or bool(_PRICE_HINT_RE.search(low))
    missing: list[str] = []
    if is_price:
        if not any(k in low for k in _PRICE_DISCLOSURE_KEYS):
            missing.append("price_disclosure")
    if high_stakes and _HIGH_STAKES_DISCLOSURE_KEY not in low:
        missing.append("specialist_steer")
    verdict: dict = {
        "status": "pass" if not missing else "fail",
        "style_warn": bool(missing),  # compliance flag — does NOT lower confidence
        "disclosure_scope": "price/estimate"
        if is_price and not high_stakes
        else "high_stakes"
        if high_stakes
        else "none",
        "missing": missing,
    }
    return verdict


def _robot_phrase_verdict(answer: str) -> dict:
    """Story 4.2 robot-phrase scan (prompt §GIỌNG VĂN banned clusters) + em-dash check.

    Flags style_warn only — confidence stays = numeric/citation grounding (plan §3.3.2).
    """
    low = (answer or "").lower()
    hit = [p for p in _ROBOT_PHRASES if p in low]
    em_dash = _EM_DASH in (answer or "")
    return {
        "status": "pass" if not hit and not em_dash else "warn",
        "style_warn": bool(hit) or em_dash,
        "robot_phrases": hit,
        "em_dash": em_dash,
    }


async def guard_output(
    answer: str,
    facts: list[dict],
    sources: list[dict],
    routing: dict | None,
    meta: dict | None = None,
) -> GuardResult:
    """Run the four L4 checks, then confidence/requires_review (never raises)."""
    meta = meta or {}
    verdicts: dict[str, Any] = {}

    # (a) numeric grounding — every number in the answer matches FACT_EVIDENCE
    evidence = evidence_values(facts)
    amounts = extract_amounts(answer)
    orphan = [a for a in amounts if not _byte_match(a, evidence)]
    verdicts["numeric_grounding"] = "pass" if not orphan else "fail"
    verdicts["orphan_numbers"] = str(orphan[:10]) if orphan else []
    numeric_pass = not orphan

    # (b) citation grounding best-effort
    verdicts["citation_grounding"] = _citation_grounding(answer, sources, [f.get("fe_id") for f in facts or []])

    # (c) confidence 3-tier
    sql_row_count = int(meta.get("sql_row_count", 0) or 0)
    strong_chunks = int(meta.get("strong_chunks", 0) or 0)
    degraded: list[str] = list(meta.get("degraded", []) or [])
    has_approx = bool(meta.get("has_approx", False))
    confidence = _confidence_3tier(numeric_pass, sql_row_count, strong_chunks, degraded, has_approx)

    # (d) high_stakes flag (routing carries it for high-stakes legal answers).
    # Defensive: also accept a dict with attribute access / nested meta so a
    # routing object from any path still escalates high-stakes correctly.
    high_stakes = bool((routing or {}).get("high_stakes"))
    if not high_stakes and isinstance(routing, dict):
        high_stakes = bool(routing.get("high_stakes") or meta.get("high_stakes"))

    # (e) story 4.2: contextual disclosure + robot-phrase (style checks, non-confidence)
    verdicts["disclosure"] = _contextual_disclosure_verdict(answer, has_approx, high_stakes)
    verdicts["robot_phrase"] = _robot_phrase_verdict(answer)

    # (f) requires_review: LOW + high_stakes keywords
    requires_review = confidence == "LOW" or high_stakes
    verdicts["high_stakes"] = high_stakes
    verdicts["confidence"] = confidence

    return GuardResult(confidence=confidence, requires_review=requires_review, verdicts=verdicts)
