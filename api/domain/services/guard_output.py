"""L4 output guard: numeric grounding, orphan detection, citations, confidence.

Business and financial numbers must byte-match normalized FACT_EVIDENCE; RAG
may ground only an amenity area when its label and exact source title share a clause.
Orphans force LOW and review, citation grounding is best-effort.
Plain async — testable without a framework.
"""

from __future__ import annotations

import logging
import re
import unicodedata
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
# Both em-dash (—) and the visually identical en-dash (–) are display-banned;
# LLM output drifts to en-dash, so normalization must cover both.
_EM_DASH = "—"
_EN_DASH = "–"
# Price/estimate answer indicator: value words OR has_approx meta flag (set by workflow).
_PRICE_HINT_RE = re.compile("(giá|tỷ đồng|triệu đồng|đồng\\s*\\(|định hướng|m²|m2)", re.IGNORECASE)
# --- Raw-LaTeX guard (prompt §GIỌNG VĂN) ---------------------------------------
# The answer LLM occasionally emits LaTeX ("\(x = 5\)", "\frac{a}{b}") which the
# chat UI renders as raw markup. The prompt now bans it; these patterns back that
# rule with a NON-DESTRUCTIVE rewrite at the joined-answer boundary only (never
# per token — see sanitize_output for the token-safe dash rule):
#   * only delimiters/commands are rewritten or dropped, no text is removed;
#   * braced forms are matched non-recursively ([^{}]*) so a stray/unbalanced
#     brace is left untouched instead of being swallowed;
#   * unknown commands survive verbatim rather than being guessed at.
_LATEX_DELIM_RE = re.compile(r"\\\[|\\\]|\\\(|\\\)|\$\$")
_LATEX_FRAC_RE = re.compile(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_LATEX_TEXT_RE = re.compile(r"\\(?:text|textrm|mathrm|operatorname)\s*\{([^{}]*)\}")
_LATEX_ENV_RE = re.compile(r"\\(?:begin|end)\s*\{[^{}]*\}")
_LATEX_LR_RE = re.compile(r"\\(?:left|right)(?![A-Za-z])")
# Only unambiguous, lossless symbol swaps — anything not listed is left as-is.
_LATEX_CMD_MAP = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠",
    "approx": "≈", "rightarrow": "→", "Rightarrow": "→", "to": "→",
    "degree": "°", "circ": "°", "dots": "…", "ldots": "…",
}
_LATEX_CMD_RE = re.compile(r"\\(%s)(?![A-Za-z])" % "|".join(sorted(_LATEX_CMD_MAP, key=len, reverse=True)))
# Escaped literals: "\%" -> "%" (the backslash is markup, the char is content).
_LATEX_ESCAPE_RE = re.compile(r"\\([%&_#$])")
# Detection only — flags leftover markup the rewrite does not cover (e.g. "\alpha").
_LATEX_MARKER_RE = re.compile(r"\\\[|\\\]|\\\(|\\\)|\$\$|\\[A-Za-z]+|\\%")


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
    """Normalize Vietnamese numeric notation, preserving short dot decimals."""
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
    elif t.count(".") > 1:
        t = t.replace(".", "")
    elif "." in t:
        whole, fraction = t.split(".")
        if len(fraction) not in {1, 2}:
            t = whole + fraction
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    if unit:
        d *= _VN_UNITS[unit]
    return d


def _extract_amount_matches(text: str) -> list[tuple[Decimal, str | None, int, int]]:
    """Extract normalized values with units and answer spans."""
    answer = text or ""
    out: list[tuple[Decimal, str | None, int, int]] = []
    for match in _NUMBER_RE.finditer(answer):
        d = _normalize_amount_text(match.group(0))
        if d is None:
            continue
        unit = match.group(2)
        if unit is None and abs(d) < _FINANCIAL_MIN:
            continue
        out.append((d, unit.lower() if unit else None, match.start(), match.end()))
    return out


def extract_amounts(text: str) -> list[Decimal]:
    """Extract normalized financial amounts from the answer."""
    return [amount for amount, _, _, _ in _extract_amount_matches(text)]


_AMENITY_LABELS = {
    "nha_tre": re.compile(r"(?<!\w)nha\s+tre(?!\w)"),
    "shcd_tang2": re.compile(r"(?<!\w)(?:shcd|sinh\s+hoat\s+cong\s+dong)\s+tang\s*2(?!\w)"),
    "shcd_tang3": re.compile(r"(?<!\w)(?:shcd|sinh\s+hoat\s+cong\s+dong)\s+tang\s*3(?!\w)"),
    "be_boi": re.compile(r"(?<!\w)be\s+boi(?!\w)"),
    "gym": re.compile(r"(?<!\w)gym(?!\w)"),
    "yoga": re.compile(r"(?<!\w)yoga(?!\w)"),
}
_CLAUSE_PUNCTUATION = ".;:!?。；！？—–\r\n"


def _normalize_clause_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _normalize_label_text(text: str) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(char)
    ).replace("đ", "d").replace("Đ", "D")
    return " ".join(text.casefold().split())


def _clause_for_span(text: str, start: int, end: int) -> str:
    """Return the punctuation-bounded clause containing a numeric span."""
    left = start
    while left:
        char = text[left - 1]
        if char in _CLAUSE_PUNCTUATION:
            break
        left -= 1
    right = end
    while right < len(text):
        char = text[right]
        if char in _CLAUSE_PUNCTUATION:
            break
        right += 1
    return _normalize_clause_text(text[left:right])


def _rag_area_match(
    amount: Decimal, unit: str | None, clause: str, rag_area_evidence: list[tuple],
) -> bool:
    """Match only an explicitly labelled, same-source amenity-area claim."""
    if unit not in {"m2", "m²"} or not isinstance(rag_area_evidence, (list, tuple)):
        return False
    labels = [label for label, pattern in _AMENITY_LABELS.items() if pattern.search(_normalize_label_text(clause))]
    if len(labels) != 1:
        return False
    label = labels[0]
    for evidence in rag_area_evidence:
        if not isinstance(evidence, (tuple, list)) or len(evidence) != 5:
            continue
        evidence_amount, evidence_label, _, source_title, _ = evidence
        if evidence_label != label or _to_decimal(evidence_amount) != amount or not isinstance(source_title, str):
            continue
        title = _normalize_clause_text(source_title)
        if title and re.search(
            r"(?<!\w)" + r"\s*[,;:]?\s*".join(re.escape(part) for part in title.split()) + r"(?!\w)",
            clause,
        ):
            return True
    return False

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
    # Capture the bare id: the brackets are markup, and comparing them against
    # `known` (which holds plain fe_ids) reported EVERY citation as missing.
    fe_cites = re.findall(r"\[(fe-\d{3})\]", answer or "")
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
    geo_places: int = 0,
) -> str:
    """HIGH when grounding passes and SQL has >= 1 row or >= 2 chunks at rerank >= 0.8;

    MEDIUM with a single source, degraded, or approx; LOW when not passing.

    A pure location query has no financial numbers to ground (numeric_pass is
    False by design); its grounding is the places catalog, so geo_places >= 1
    substitutes for SQL/chunk provenance instead of forcing LOW.
    """
    # A pure location query has no financial numbers to ground, so numeric_pass
    # is True by default; if orphan financial numbers ARE present, they still
    # force LOW regardless of geo grounding.
    if not numeric_pass:
        return "LOW"
    if geo_places >= 1:
        # Location answer grounded by the places catalog, not financial evidence.
        if degraded and "geo_degraded" in degraded:
            return "LOW"
        return "MEDIUM"
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
    dash = _EM_DASH in (answer or "") or _EN_DASH in (answer or "")
    return {
        "status": "pass" if not hit and not dash else "warn",
        "style_warn": bool(hit) or dash,
        "robot_phrases": hit,
        "em_dash": dash,
    }


def sanitize_output(text: str) -> str:
    """Normalize display-banned dashes (em — and en –) to a plain hyphen.

    Applied to the final answer before storage/emission so the rendered message
    never shows the banned dash glyphs. Kept in this module (next to the check)
    so flagging and normalization stay in one place.
    """
    return (text or "").replace(_EM_DASH, "-").replace(_EN_DASH, "-")


def normalize_answer_display(text: str) -> str:
    """Rewrite raw LaTeX in a COMPLETED answer to readable Unicode/plain math.

    Applied once to the joined answer (workflow generate step), never to streamed
    tokens, so SSE emission keeps its exact token-by-token semantics. Purely
    additive/substitutive: delimiters and known commands are rewritten, unknown
    markup is left verbatim, and no content is dropped or truncated.
    """
    if not text:
        return text or ""
    out = _LATEX_FRAC_RE.sub(r"\1/\2", text)
    out = _LATEX_TEXT_RE.sub(r"\1", out)
    out = _LATEX_ENV_RE.sub("", out)
    out = _LATEX_LR_RE.sub("", out)
    out = _LATEX_CMD_RE.sub(lambda m: _LATEX_CMD_MAP.get(m.group(1), m.group(0)), out)
    out = _LATEX_ESCAPE_RE.sub(r"\1", out)
    return _LATEX_DELIM_RE.sub("", out)


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

    # (a) numeric grounding — SQL facts remain authoritative for all numbers;
    # RAG can ground only a labelled amenity area tied to its exact source title.
    evidence = evidence_values(facts)
    amount_matches = _extract_amount_matches(answer)
    orphan = [
        amount
        for amount, unit, start, end in amount_matches
        if not _byte_match(amount, evidence)
        and not _rag_area_match(
            amount,
            unit,
            _clause_for_span(answer, start, end),
            meta.get("_rag_area_evidence", []),
        )
    ]
    verdicts["numeric_grounding"] = "pass" if not orphan else "fail"
    verdicts["orphan_numbers"] = str(orphan[:10]) if orphan else []
    numeric_pass = not orphan

    # (b) citation grounding best-effort
    citation_verdict = _citation_grounding(
        answer, sources, [f.get("fe_id") for f in facts or []]
    )
    verdicts["citation_grounding"] = citation_verdict
    # A citation naming an evidence id that does not exist is a VERIFIABLE
    # grounding failure (unlike the "pending" verdict when there is no span to
    # check), so it forces LOW exactly the way an orphan number does — and LOW
    # is what raises requires_review below.
    citation_fail = citation_verdict.get("status") == "fail"

    # (c) confidence 3-tier
    sql_row_count = int(meta.get("sql_row_count", 0) or 0)
    strong_chunks = int(meta.get("strong_chunks", 0) or 0)
    degraded: list[str] = list(meta.get("degraded", []) or [])
    has_approx = bool(meta.get("has_approx", False))
    geo_places = int(meta.get("geo_places", 0) or 0)
    confidence = _confidence_3tier(
        numeric_pass and not citation_fail,
        sql_row_count,
        strong_chunks,
        degraded,
        has_approx,
        geo_places,
    )

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
