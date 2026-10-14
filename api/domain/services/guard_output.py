"""L4 output guard: numeric grounding, orphan detection, citations, confidence.

A number in the answer is authorized by exactly three paths, strongest first:
an exact FACT_EVIDENCE value (byte-match after normalization), a labelled amenity
area tied to its exact source title inside the same clause, or — for NON-money
figures only — the text of a chunk that already passed scope validation. Money
never travels on prose: exact sale prices exist for only a few units, so a price
may be authorized by a registry fact and by nothing else. The chunk path is
itself vetoed when the clause names an amenity the registry already binds to a
different value, because presence in a chunk cannot bind a number to the amenity
the answer names. Orphans force LOW and review, citation grounding is best-effort.
Plain async — testable without a framework.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

logger = logging.getLogger("api.guard_output")

# Numbers with Vietnamese units (tỷ/triệu/ngàn + %/m2); "financial" only when a
# unit is present or the magnitude is >= 1M (skips articles, years, floors).
# The currency spellings carry a word boundary because "đ" is also the first
# letter of extremely common words ("điều", "đợt", "đơn", "đầu", "đến", "đính")
# and "\s*" allows the separating space, so "khoản 1 điều 152" tokenized as the
# money figure "1 đ" — and money is only ever authorized by a fact, so an answer
# faithfully quoting the article was canned as numeric_ungrounded. "đồng" and
# "vnđ" take the boundary for the same reason ("đồng thời", "đồng ý"). A number
# whose unit fails the boundary falls through as unit=None, which
# _extract_amount_matches drops unless the magnitude reaches _FINANCIAL_MIN.
_NUMBER_RE = re.compile(
    r"(\d[\d.,]*)\s*(tỷ|tỉ|triệu|ngàn|nghìn|đồng(?!\w)|đ(?!\w)|vnđ(?!\w)|%|m2|m²)?",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(
    r"(?<![\w])(?:CH-\d{1,2}[A-Z]?|\[fe-\d{3}\])", re.IGNORECASE
)
# A range that states its unit once: "1,72 – 2,30 tỷ", "32,5 – 68,4 m2". Group 1
# is the LEFT bound (which inherits the unit), group 3 the unit stated on the
# right bound. The dash is the en/em range separator the writer actually uses.
_RANGE_SHARED_UNIT_RE = re.compile(
    r"(\d[\d.,]*)\s*(?:[\u2013\u2014-]|đến|toi|to)\s*(\d[\d.,]*)\s*"
    r"(tỷ|tỉ|triệu|ngàn|nghìn|đồng|đ|vnđ|m2|m²)",
    re.IGNORECASE,
)
_FINANCIAL_MIN = 1_000_000

_VN_UNITS = {
    "tỷ": Decimal("1e9"),
    # "tỉ" is the Southern spelling of the same unit and is listed as currency by
    # price_calc and by the workflow's _FIGURE_UNITS. Leaving it out here made
    # "2 tỉ" normalize to 2 — an orphan against a 2e9 fact, i.e. a correct price
    # quoted from a fact read as a hallucination and the answer was withheld.
    "tỉ": Decimal("1e9"),
    "triệu": Decimal("1e6"),
    "ngàn": Decimal("1e3"),
    "nghìn": Decimal("1e3"),
}

# Units that make a figure a MONEY claim. Mirrors the workflow's _FIGURE_UNITS
# minus "%": a percentage, an area and a bare count are dimensions a validated
# chunk may legitimately state, while a currency figure may only come from a fact.
_CURRENCY_UNITS = frozenset(
    {"tỷ", "tỉ", "triệu", "ngàn", "nghìn", "đồng", "đ", "vnđ"}
)

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


# --- Totality boundary ---------------------------------------------------------
# The guard runs on the customer's answer path, where an escaping exception is not
# a diagnostic but a data loss: the caller converts it into ``verification_failed``
# and substitutes a canned string for an answer retrieval had already grounded.
# These coercions are therefore the input boundary: everything below them may
# assume the documented types, and nothing below them may assume anything about
# what the caller actually passed.


def _as_text(value: Any) -> str:
    """Coerce any input to ``str`` without raising."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — a hostile __str__ must not break the guard
        return ""


def _as_dict_list(value: Any) -> list[dict]:
    """Coerce a fact/source collection to a list of mappings.

    A bare mapping is accepted as a single item: callers that hand one fact or one
    source instead of a list mean the same thing, and iterating its keys (the old
    behaviour) only ever produced an AttributeError.
    """
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_int(value: Any, default: int = 0) -> int:
    """Coerce a count-like input to ``int`` without raising."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_str_list(value: Any) -> list[str]:
    """Coerce a flag container to a list of strings without raising."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_as_text(item) for item in value]
    return []


def _field(container: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping OR an object, without raising.

    Routing and meta arrive from several call paths, and a routing object with
    attributes must escalate high-stakes exactly like the dict does; a broken or
    unexpected container degrades to ``default`` instead of raising.
    """
    try:
        if isinstance(container, dict):
            return container.get(key, default)
        getter = getattr(container, "get", None)
        if callable(getter):
            return getter(key, default)
        return getattr(container, key, default)
    except Exception:  # noqa: BLE001 — an unreadable field is an absent field
        return default


def _to_decimal(v: Any) -> Decimal | None:
    """Normalize a value to Decimal — safe for numbers beyond 2^53.

    Returns None for anything that is not a finite number: a non-finite Decimal
    (NaN/Infinity) compares with ``<`` by raising InvalidOperation, so letting one
    through would turn a malformed fact value into an exception on the answer path.
    """
    try:
        if isinstance(v, bool):
            return None
        if isinstance(v, Decimal):
            d = v
        elif isinstance(v, int):
            d = Decimal(v)
        elif isinstance(v, float):
            d = Decimal(str(v))
        elif isinstance(v, str):
            s = v.strip().replace("\u00a0", "").replace(" ", "")
            if not s:
                return None
            d = Decimal(s)
        else:
            # Numeric types outside int/float (e.g. numpy integers) still carry an
            # exact decimal spelling; anything else fails the parse and is dropped.
            d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def _decimal_set(value: Any) -> set[Decimal]:
    """Coerce a numeric-evidence container to a set of finite Decimals.

    Built once per guard call so each figure costs one hash lookup instead of a
    linear scan of the whole evidence projection. The scan made the guard
    quadratic in the number of figures: with a large fact projection, an answer
    carrying a few hundred figures crossed the 1.5s step budget on its own.
    """
    if isinstance(value, (set, frozenset)):
        return {
            item for item in value
            if isinstance(item, Decimal) and item.is_finite()
        }
    if not isinstance(value, (list, tuple)):
        return set()
    out: set[Decimal] = set()
    for item in value:
        d = item if isinstance(item, Decimal) else _to_decimal(item)
        if d is not None:
            out.add(d)
    return out


def _normalize_amount_text(text: str) -> Decimal | None:
    """Normalize Vietnamese numeric notation, preserving short dot decimals.

    Total by construction: this runs once per candidate figure inside the guard,
    so a malformed or non-finite spelling returns None instead of raising.
    """
    try:
        t = _as_text(text).strip().lower()
        unit = None
        for u in _VN_UNITS:
            if u in t:
                unit = u
                t = t.replace(u, "").strip()
                break
        t = t.replace("%", "").replace("m2", "").replace("m²", "").replace("đồng", "").strip()
        # "đ" and "vnđ" are currency suffixes too, and they are stripped from the
        # END of the token only: "đ" is a letter that also begins "đồng" and occurs
        # inside other words, so a global replace would corrupt the number. "vnđ"
        # is stripped before "đ" because it ends with it. Leaving these two out
        # made "2.850.000.000 đ" normalize to None, so the figure was dropped
        # before _is_money_claim could refuse it and an unbacked price shipped.
        for suffix in ("vnđ", "đ"):
            if t.endswith(suffix):
                t = t[: -len(suffix)].strip()
                break
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
        d = Decimal(t)
        if unit:
            d *= _VN_UNITS[unit]
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def _range_shared_unit_spans(
    text: str, covered: set[tuple[int, int]]
) -> list[tuple[Decimal, str, int, int]]:
    """Left bounds that inherit their unit from the range's right bound.

    Vietnamese writes a band with the unit stated once ("1,72 – 2,30 tỷ",
    "32,5 – 68,4 m2"). The left bound is then a bare decimal below
    ``_FINANCIAL_MIN``, so the main pass drops it: the range arrives as one
    figure, no two-bound claim can form, and a correct answer is refused as an
    unsupported number. The unit is read from the right bound and applied to the
    left one; a bound that states its own unit never matches here.
    """
    out: list[tuple[Decimal, str, int, int]] = []
    for match in _RANGE_SHARED_UNIT_RE.finditer(text):
        start, end = match.span(1)
        if any(start >= s and end <= e for s, e in covered):
            continue
        unit = match.group(3).lower()
        value = _normalize_amount_text(f"{match.group(1)} {unit}")
        if value is None:
            continue
        out.append((value, unit, start, end))
    return out


def _extract_amount_matches(text: str) -> list[tuple[Decimal, str | None, int, int]]:
    """Extract normalized values with units and answer spans."""
    answer = _as_text(text)
    identifiers = list(_IDENTIFIER_RE.finditer(answer))
    # Both sequences are in ascending order, so the identifiers that can overlap a
    # numeric span form one contiguous window: the first one ending after the span
    # starts, through the last one starting before it ends. Testing that window
    # beats re-testing every identifier per figure, which was quadratic on a long
    # answer that carries many ids and many numbers.
    id_starts = [identifier.start() for identifier in identifiers]
    id_ends = [identifier.end() for identifier in identifiers]
    out: list[tuple[Decimal, str | None, int, int]] = []
    for match in _NUMBER_RE.finditer(answer):
        start, end = match.start(), match.end()
        window = bisect_right(id_ends, start)
        if window < len(id_starts) and id_starts[window] < end:
            continue
        d = _normalize_amount_text(match.group(0))
        if d is None:
            continue
        unit = match.group(2)
        if unit is None and abs(d) < _FINANCIAL_MIN:
            continue
        out.append((d, unit.lower() if unit else None, start, end))
    inherited = _range_shared_unit_spans(answer, {(s, e) for _, _, s, e in out})
    if inherited:
        out.extend(inherited)
        out.sort(key=lambda span: span[2])
    return out


def extract_amounts(text: str) -> list[Decimal]:
    """Extract financial amounts, excluding digits inside unit/citation IDs."""
    return [amount for amount, _, _, _ in _extract_amount_matches(text)]


_AMENITY_LABELS = {
    # Separators are [_\\s]+ rather than \\s+: the registry chunk writes these
    # labels with underscores (nha_tre, be_boi) while an answer writes them with
    # spaces, and _normalize_label_text only strips diacritics — it never maps
    # "_" to " ". A whitespace-only pattern therefore matched 2 of 6 labels and
    # silently orphaned the rest.
    "nha_tre": re.compile(r"(?<!\w)nha[\s_]+tre(?!\w)"),
    "shcd_tang2": re.compile(r"(?<!\w)(?:shcd|sinh[\s_]+hoat[\s_]+cong[\s_]+dong)[\s_]+tang[\s_]*2(?!\w)"),
    "shcd_tang3": re.compile(r"(?<!\w)(?:shcd|sinh[\s_]+hoat[\s_]+cong[\s_]+dong)[\s_]+tang[\s_]*3(?!\w)"),
    "be_boi": re.compile(r"(?<!\w)be[\s_]+boi(?!\w)"),
    "gym": re.compile(r"(?<!\w)gym(?!\w)"),
    "yoga": re.compile(r"(?<!\w)yoga(?!\w)"),
    # Not a registry amenity: it lives here because this dict is what makes a
    # matched shape canonical, and the chunk text binds a balcony area to its own
    # subject ("CH-03 ... 68.3 m2 thông thủy, ban công 5.2 m2"). Without it the
    # balcony figure inherits the unit code and is vetoed as a contradiction.
    "ban_cong": re.compile(r"(?<!\w)ban[\s_]+cong(?!\w)"),
}
# "," is deliberately NOT a clause boundary: one price clause spans it
# ("2.850.000.000 đồng, tức 2,85 tỷ"), so splitting there would orphan the price.
# The comma-joined amenity list is handled inside _rag_area_match instead.
_CLAUSE_PUNCTUATION = ".;:!?。；！？\r\n"
_CLAUSE_BREAK_RE = re.compile("[%s]" % re.escape(_CLAUSE_PUNCTUATION))
# A dash BETWEEN two digits is a range separator, not a clause boundary: a writer
# states one price band as "1.720.000.000 – 2.300.000.000" or "1,5–2 tỷ", and
# breaking there splits one claim into two single-figure clauses that carry
# neither the range's label nor its second bound, so the whole band is orphaned
# and the answer is refused as an unsupported figure. Dashes elsewhere stay
# boundaries, matching the prose use ("— lưu ý").
_DASH_CHARS = "\u2014\u2013"
# Anchored on the dash itself so the reported position is the dash, not the
# leading whitespace: _clause_breaks compares that position against the
# boundary set, and a range separator must be excluded from it.
# The neighbour may be a digit or the mask character: _financial_claims blanks
# every numeric span with "\x00" before asking for clause boundaries, so a range
# arriving from that path has "\x00 \u2013 \x00" around the dash and a
# digit-only lookbehind would never match it.
_RANGE_DASH_RE = re.compile(
    "(?<=[\\d\\x00])[\\s]*([%s])[\\s]*(?=[\\d\\x00])" % _DASH_CHARS
)
_DASH_BREAK_RE = re.compile("[%s]" % _DASH_CHARS)
# A "." between digits is a thousands separator, not a sentence end: Vietnamese
# writes 1.720.000.000, and treating each dot as a boundary shredded the figure
# into unparseable fragments. Same class of defect as the range dash above.
_THOUSANDS_DOT_RE = re.compile(r"(?<=\d)\.(?=\d)")
# A ":" that closes a LABEL immediately before a figure is not a clause
# boundary either: an answer formats its rows as markdown ("*Phương án HTLS:*
# 1.980.000.000 đồng – 2.640.000.000 đồng"), and breaking at that colon hands
# the figures a clause with neither the price cue nor the estimate label, so
# both bounds are refused although the row is correct. A colon followed by
# prose stays a boundary ("Lưu ý: giá có thể thay đổi").
_LABEL_COLON_RE = re.compile(r":(?=[\s*_]*[\d\x00])")
_COLON_BREAK_RE = re.compile(":")
# A clause carries a figure when a number is followed by an area unit; used to
# decide whether an amenity label in that clause actually governs an area.
_AMOUNT_FOR_LABEL_RE = re.compile(r"\d[\d.,]*\s*m(?:2|²)(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=256)
def _normalize_clause_text(text: str) -> str:
    return " ".join(_as_text(text).casefold().split())


@lru_cache(maxsize=256)
def _normalize_label_text(text: str) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFD", _as_text(text))
        if not unicodedata.combining(char)
    ).replace("đ", "d").replace("Đ", "D")
    return " ".join(text.casefold().split())


@lru_cache(maxsize=8)
def _clause_breaks(text: str) -> tuple[int, ...]:
    """Positions of clause punctuation in ``text``.

    Cached because the guard asks for one clause per orphan figure: rescanning the
    whole answer character by character for every orphan made the check quadratic
    on a long, sparsely punctuated answer.

    A dash joining two digits is skipped: it separates the bounds of ONE range
    ("1.720.000.000 – 2.300.000.000"), so treating it as a boundary would split a
    single labelled claim into two unlabelled single-figure clauses and orphan
    both bounds. Every other dash is a boundary as before.
    """
    joined = {match.start(1) for match in _RANGE_DASH_RE.finditer(text)}
    thousands = {match.start() for match in _THOUSANDS_DOT_RE.finditer(text)}
    labels = {match.start() for match in _LABEL_COLON_RE.finditer(text)}
    breaks = [
        match.start()
        for match in _CLAUSE_BREAK_RE.finditer(text)
        if match.start() not in thousands and match.start() not in labels
    ]
    breaks.extend(
        match.start()
        for match in _DASH_BREAK_RE.finditer(text)
        if match.start() not in joined
    )
    return tuple(sorted(breaks))


def _clause_for_span(
    text: str, start: int, end: int, source: str | None = None
) -> str:
    """Return the punctuation-bounded clause containing a numeric span.

    ``source`` is the text the clause is SLICED from when it differs from the
    text the boundaries were computed on. The caller masks every figure before
    asking for boundaries (so a thousands-separated amount is not punctuation),
    and slicing that mask would hand back "phuong an thanh toan som \\x00\\x00"
    for a policy named "…sớm 95%" — the label no longer matches anything, so the
    clause lost the very cue it states. Boundaries from the mask, characters
    from the original.
    """
    answer = _as_text(text)
    # A span from a stale or external caller must clamp, never index: the previous
    # character walk raised IndexError on an out-of-range start.
    start = max(0, min(_as_int(start), len(answer)))
    end = max(start, min(_as_int(end), len(answer)))
    breaks = _clause_breaks(answer)
    cut = bisect_left(breaks, start)
    left = breaks[cut - 1] + 1 if cut else 0
    right_index = bisect_left(breaks, end)
    right = breaks[right_index] if right_index < len(breaks) else len(answer)
    body = answer if source is None else _as_text(source)
    if len(body) != len(answer):
        # A source of a different length cannot be sliced with these offsets;
        # fall back to the boundary text rather than returning a shifted clause.
        body = answer
    return _normalize_clause_text(body[left:right])


def _is_money_claim(amount: Decimal, unit: str | None) -> bool:
    """True when a figure states an amount of money rather than a dimension.

    Money is the one class of figure that may not be authorized by prose: exact
    sale prices exist for only a few units, so a chunk that merely mentions the
    figure is not evidence of it. Dimensions (area, percentage, count) are safe to
    take from validated chunk text.
    """
    if isinstance(unit, str) and unit in _CURRENCY_UNITS:
        return True
    return unit is None and abs(amount) >= _FINANCIAL_MIN


# --- Chunk-text subject bindings ------------------------------------------------
# A figure quoted from a chunk is only safe to authorize when the chunk does not
# attribute that subject to a different value. The registry-backed amenity table
# covers one section and six labels, so it missed the whole class where the
# subject is a unit type ("2PN", "Studio", "CH-03"): the chunk states 2PN is
# 68,4 m², the answer says 2PN is 32,5 m², and mere presence of "32.5" in the
# chunk (as the Studio area) authorized the false claim. These helpers recover
# the (area, subject) pairs the chunk text itself states, for every section and
# for any subject, so the veto can compare subjects instead of numbers.
_AREA_BINDING_RE = re.compile(r"\d[\d.,]*\s*m(?:2|²)(?!\w)", re.IGNORECASE)
# A binding is LOCAL: a separator ends a subject's reach, so in
# "… 68.4 m2, ban cong 5.2 m2" the first subject never governs the second figure.
# ":" and "/" are deliberately NOT separators: the real registry writes
# "CH-09: 28.4 m2 thong thuy / 32.0 m2 tim tuong", and breaking there would lose
# the unit code that actually governs both figures.
_SEGMENT_BREAKS = frozenset(",;*•|·()[]{}\n\r\t")
_LABEL_WINDOW = 120

# Subjects are matched by SHAPE, not by "the token just before the number".
# Measured on the real corpus, the token rule bound 24 of 48 pairs to the filler
# "thuy" (from "thong thuy"), plus "chuan", "ho", "dong", "m2" and "71m2" — junk
# subjects that would veto any answer merely mentioning those words. A subject is
# a unit code, a unit type, a unit kind, or a named amenity; anything else leaves
# the figure unbound, which vetoes nothing.
_SUBJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("code", re.compile(r"(?<!\w)ch-\d{1,3}[a-z]?(?!\w)")),
    ("type", re.compile(r"(?<!\w)\d{1,2}\s*pn(?!\w)")),
    ("kind", re.compile(r"(?<!\w)(?:studio|penthouse|duplex|shophouse|officetel)(?!\w)")),
) + tuple(_AMENITY_LABELS.items())


def _figure_segment(text: str, start: int) -> str:
    """Text between the nearest preceding separator and a figure."""
    lower = max(0, start - _LABEL_WINDOW)
    index = start
    while index > lower and text[index - 1] not in _SEGMENT_BREAKS:
        index -= 1
    return text[index:start]


def _subject_label(segment: str) -> str | None:
    """The subject nearest to the figure in ``segment``, or None when unbound.

    Runs on the normalized segment so accented prose ("bể bơi") matches the same
    pattern as the registry spelling ("be_boi"), and returns a canonical label the
    clause-side tokenizer can compare against.
    """
    normalized = _normalize_label_text(segment)
    best_key: tuple[int, int] | None = None
    best_label: str | None = None
    for canonical, pattern in _SUBJECT_PATTERNS:
        for match in pattern.finditer(normalized):
            key = (match.end(), match.start())
            if best_key is None or key > best_key:
                best_key = key
                text = "".join(match.group(0).split())
                # The amenity patterns name their canonical form; the shape
                # patterns use whatever they matched (a unit code keeps its id).
                best_label = canonical if canonical in _AMENITY_LABELS else text
    return best_label


@lru_cache(maxsize=256)
def _clause_subjects(clause: str) -> frozenset[str]:
    """Canonical subjects the clause names, extracted the way the chunk side does.

    Symmetric with _subject_label on purpose: the label the chunk bound and the
    label tested here must come from the same patterns, or a spaced spelling
    ("nhà trẻ") never matches the canonical underscore label ("nha_tre") and the
    veto silently stops firing — a wrong statement then ships with no fact even
    missing.

    Cached, and frozen, because every figure in a clause asks the same question:
    a long answer whose figures share one unpunctuated clause rescanned it once per
    figure, which is quadratic in the clause length.
    """
    normalized = _normalize_label_text(clause)
    subjects: set[str] = set()
    for canonical, pattern in _SUBJECT_PATTERNS:
        for match in pattern.finditer(normalized):
            # Same canonical rule as _subject_label: the amenity patterns name
            # their own form, the shape patterns use whatever they matched.
            subjects.add(
                canonical if canonical in _AMENITY_LABELS else "".join(match.group(0).split())
            )
    return frozenset(subjects)


def _label_intro_subject(text: str, start: int) -> str | None:
    """Subject of the nearest "label:" preceding a figure, or None.

    The nearest separator is not always the nearest governor. A range written as
    "be_boi: 300 m² (A1) đến 500 m² (D)" loses its second figure, because "(" and
    ")" are separators, so the walk stops on " đến " and the figure stays unbound —
    and an unbound figure makes a FAITHFUL answer look ungrounded. This fallback
    asks instead which label the corpus introduced last, which is the governor the
    corpus actually writes.

    The caller decides whether that label may be used: reading a label here says
    only who the corpus put in charge, not whether this module already prices them.
    """
    lower = max(0, start - _LABEL_WINDOW)
    colon = text.rfind(":", lower, start)
    if colon <= lower:
        return None
    index = colon
    while index > lower and text[index - 1] not in _SEGMENT_BREAKS:
        index -= 1
    return _subject_label(text[index:colon])


def _chunk_binding_evidence(rows: list[dict]) -> list[tuple[Decimal, str]]:
    """(area, subject) pairs that the validated chunk text itself binds together.

    Every section is read, not only the amenity one, and the subject is whatever
    unit code, unit type, unit kind or named amenity governs the figure — the leak
    being closed was a unit-type subject, which no amenity label could ever match.
    Only an area unit counts, so counts and percentages are never bound. Malformed
    input yields [] rather than raising.

    A figure the separator walk cannot reach is offered to its nearest "label:",
    but only when the chunk ALREADY prices that label through the walk. The two
    directions are not symmetric, and this is the whole point: extending a priced
    subject can only make a faithful answer harder to refuse, while pricing a
    subject the chunk never priced adds a veto that did not exist. Measured on the
    real corpus, pricing the type labels outright turned five verbatim clauses into
    rejections — "26,72 - 38,15m2 - căn hộ 1pn" names 1PN and claims a range that
    belongs to Studio — and that is a real leak, but closing it is a veto-side
    change with its own blast radius, not this one. So a label this chunk never
    priced keeps its figures unbound, and stays vetoed by presence instead.
    """
    out: list[tuple[Decimal, str]] = []
    for row in _as_dict_list(rows):
        content = row.get("content")
        if not isinstance(content, str):
            continue
        primary: list[tuple[Decimal, str]] = []
        unbound: list[tuple[Decimal, int]] = []
        for match in _AREA_BINDING_RE.finditer(content):
            amount = _normalize_amount_text(match.group(0))
            if amount is None or amount <= 0:
                continue
            label = _subject_label(_figure_segment(content, match.start()))
            if label:
                primary.append((amount, label))
            else:
                unbound.append((amount, match.start()))
        out.extend(primary)
        priced = {label for _amount, label in primary}
        if not priced:
            continue
        for amount, start in unbound:
            label = _label_intro_subject(content, start)
            if label and label in priced:
                out.append((amount, label))
    return out


def _bound_values_by_label(
    clause: str, area_evidence: Any, binding_evidence: Any,
) -> dict[str, set[Decimal]]:
    """Values each subject is bound to, merged from the registry and the chunk.

    Both sources describe AREA figures, so a subject's values are collected before
    any decision: one subject legitimately carries several areas ("CH-01: 68.3 m2
    thông thủy / 73.8 m2 tim tường"), and deciding per binding rather than per
    subject vetoed 54 of 62 faithful quotes on the real corpus.

    The registry half keeps its original precondition — it only speaks about a
    clause that makes an area claim — while the chunk half needs no such gate,
    because its bindings are area-only by construction.
    """
    bound: dict[str, set[Decimal]] = {}
    if isinstance(area_evidence, (list, tuple)) and _AMOUNT_FOR_LABEL_RE.search(clause):
        for entry in area_evidence:
            if not isinstance(entry, (tuple, list)) or len(entry) not in {5, 6}:
                continue
            value, label = entry[0], entry[1]
            if not isinstance(label, str) or not label:
                continue
            parsed = _to_decimal(value)
            if parsed is not None:
                bound.setdefault(label, set()).add(parsed)
    if isinstance(binding_evidence, (list, tuple)):
        for entry in binding_evidence:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                continue
            value, label = entry[0], entry[1]
            if not isinstance(label, str) or not label:
                continue
            parsed = _to_decimal(value)
            if parsed is not None:
                bound.setdefault("".join(label.split()), set()).add(parsed)
    return bound


# Veto-side list separators, deliberately NOT the same set as ``_SEGMENT_BREAKS``.
# That one serves the BINDING direction, where "/" must not break: the registry
# writes "CH-09: 28.4 m2 thong thuy / 32.0 m2 tim tuong" and the code has to keep
# governing both figures. Here the split only isolates a (subject, figure) pair the
# ANSWER itself states, and a segment carrying no subject is skipped, so the
# registry's own "/" form stays publishable while "gym 434,2 m²" next to another
# subject stops being shielded by its neighbour's value.
_VETO_SEPARATORS = frozenset("|/")


def _segment_texts(clause: str) -> list[str]:
    """Split a clause at LIST separators, leaving Vietnamese decimal commas alone.

    Splitting on every comma is wrong here, and it was measured wrong: "," is also
    the Vietnamese decimal mark, so "nha_tre 434,2 m², shcd_tang2 …" tore into
    "nha_tre 434" and "2 m²". No segment then carried a whole figure, the segment
    rule below never fired, and the leak it exists to close stayed open while the
    code read as if it were closed. A comma is a decimal mark exactly when an ASCII
    digit sits on both sides of it; every other comma separates list items.

    Only ASCII digits count: "²" satisfies ``str.isdigit``, and "… m²,2pn …" would
    otherwise be read as a decimal instead of as a separator.

    "|" and "/" are separators here too, because both join list items in the real
    corpus (a price table row uses "|", the unit registry uses "/"). Splitting only
    on "," left the same false figure shipping in those layouts — measured:
    "nha_tre 434,2 m² | gym 434,2 m²" published while the comma form was vetoed.
    """
    segments: list[str] = []
    start = 0
    for index, char in enumerate(clause):
        if char == ",":
            before = clause[index - 1] if index else ""
            after = clause[index + 1] if index + 1 < len(clause) else ""
            if "0" <= before <= "9" and "0" <= after <= "9":
                continue
        elif char not in _VETO_SEPARATORS:
            continue
        segments.append(clause[start:index])
        start = index + 1
    segments.append(clause[start:])
    return segments


@lru_cache(maxsize=256)
def _comma_segments(clause: str) -> tuple[tuple[str, frozenset[Decimal]], ...]:
    """Split a clause at commas and record the figures each segment carries.

    A comma is deliberately NOT a clause boundary — one price clause spans it
    ("2.850.000.000 đồng, tức 2,85 tỷ") — so a faithful comma-joined list of areas
    reaches the veto as ONE clause naming every subject it lists. Segmenting here,
    after the clause has already been fixed, leaves that decision intact while
    recovering the (subject, figure) pairing the list actually states.

    Cached because segmentation and the figure scan do not depend on which figure is
    being judged: a six-figure list would otherwise re-parse the same clause once per
    figure, and a long list once per figure is quadratic.
    """
    out: list[tuple[str, frozenset[Decimal]]] = []
    for segment in _segment_texts(clause):
        if not segment.strip():
            continue
        amounts = frozenset(
            amount for amount, _unit, _start, _end in _extract_amount_matches(segment)
        )
        out.append((segment, amounts))
    return tuple(out)


def _segment_value_conflict(
    amount: Decimal, clause: str, area_evidence: Any, binding_evidence: Any,
) -> bool:
    """True when a comma segment names ONE subject and prices this figure differently.

    The unified rule is clause-wide, so inside a comma list it degrades to "does any
    subject the clause names own this value": "…, gym 434,2 m², …" ships because
    nha_tre — another label on the same line — owns 434,2, even though the chunk
    states gym is 134,3 m². The identical claim in its own sentence is vetoed, so the
    verdict depended on the answer's layout rather than on whether it was true.

    Deliberately narrow, because withholding a TRUE statement is worse than the leak
    it closes: only a segment naming EXACTLY ONE subject is judged, only for a figure
    that segment itself carries, and only when that subject has a value to compare
    against. A segment naming no subject (an unlabelled figure, or a subject outside
    the known shapes) or naming several ("Căn CH-03 … và căn CH-04 …") is left to the
    presence test exactly as before.
    """
    for segment, amounts in _comma_segments(clause):
        if amount not in amounts:
            continue
        subjects = _clause_subjects(segment)
        if len(subjects) != 1:
            continue
        values = _bound_values_by_label(segment, area_evidence, binding_evidence).get(
            next(iter(subjects))
        )
        if values and amount not in values:
            return True
    return False


def _labelled_value_conflict(
    amount: Decimal, clause: str, area_evidence: Any, binding_evidence: Any,
) -> bool:
    """True when EVERY subject the clause names prices this figure differently.

    The chunk path can only prove that a number appears somewhere in a validated
    chunk; it cannot bind that number to the subject the answer names. So when the
    clause names a subject a source already prices — and that value is not this
    figure — the rejection must stand rather than being overturned by mere
    presence. Without this, "Gym rộng 434,2 m²" ships even though the chunk states
    gym is 134,3 m² and 434,2 m² belongs to the nursery: a false statement the
    guard exists to catch.

    Registry and chunk evidence are merged into ONE rule, because both describe
    area figures and both were previously read asymmetrically: the binding was
    local while the comparison was clause-wide, so every faithful comma-joined
    list ("nha_tre 434,2 m², …, shcd_tang3 232,1 m²") had each figure vetoed by the
    five sibling labels sharing its clause. A figure is a contradiction only when
    everything the clause names refuses it.

    A subject absent from both sources contradicts nothing, and a subject bound to
    this exact figure is consistent, so both are left to the presence test.

    This rule is clause-wide and must stay that way: narrowing it to the subject a
    figure sits next to is what caused the round-2 over-veto, where 54 of 62 faithful
    quotes were withheld. A false figure riding along inside a comma list is caught by
    ``_segment_value_conflict`` instead, which only ever ADDS a veto.
    """
    clause_text = _as_text(clause)
    subjects = _clause_subjects(clause_text)
    if not subjects:
        return False
    bound = _bound_values_by_label(clause_text, area_evidence, binding_evidence)
    named = {label: values for label, values in bound.items() if label in subjects}
    if named and not any(amount in values for values in named.values()):
        return True
    return _segment_value_conflict(amount, clause_text, area_evidence, binding_evidence)


def _chunk_numeric_match(
    amount: Decimal,
    unit: str | None,
    chunk_evidence: list[Decimal],
    clause: str | None,
    area_evidence: Any,
    binding_evidence: Any,
) -> bool:
    """Authorize a NON-money figure by the text of a scope-validated chunk.

    Weakest of the three authority paths and deliberately narrow: the value must
    appear exactly in evidence extracted from chunks that passed scope validation,
    money is excluded outright, and a figure every subject its clause names prices
    differently is vetoed. An empty list authorizes nothing — it is what the
    caller sends when scope or integrity validation failed.

    ``clause`` and both evidence lists are REQUIRED, with no default: a call site
    that forgets them must fail loudly. A default made the omission silent, and a
    missing argument then read as "nothing to contradict this figure" — the veto
    switched itself off exactly where a wrong number would otherwise have shipped.
    """
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return False
    if _is_money_claim(amount, unit):
        return False
    if not isinstance(clause, str):
        # Without the clause the veto cannot be evaluated, so the label-free
        # presence test must not run either: presence in a chunk cannot bind a
        # number to the subject the answer names, and granting authority that
        # cannot be checked is how a false "Gym rộng 434,2 m²" ships.
        return False
    if _labelled_value_conflict(amount, clause, area_evidence, binding_evidence):
        return False
    if isinstance(chunk_evidence, (set, frozenset)):
        # Pre-normalized by _decimal_set: one hash lookup per figure.
        return amount in chunk_evidence
    if not isinstance(chunk_evidence, (list, tuple)) or not chunk_evidence:
        return False
    return any(_to_decimal(value) == amount for value in chunk_evidence)


def _rag_area_match(
    amount: Decimal, unit: str | None, clause: str, rag_area_evidence: list[tuple],
) -> bool:
    """Match an explicitly labelled amenity-area claim against registry evidence.

    The claim must be pinned three ways: the label names exactly one amenity, the
    amount equals the registry value, and the source title is attributable WITHIN
    THE SAME CLAUSE. Clause-local attribution is what keeps a figure from being
    laundered across a sentence boundary ("Nhà trẻ rộng 434,2 m². Theo <source>."
    must NOT ground — the number and its source are separate claims).

    A comma-joined list puts several labels in one clause, so the governing label
    is the nearest one to the LEFT of the figure — the one that introduces it.
    """
    if not isinstance(unit, str) or unit not in {"m2", "m²"}:
        return False
    if not isinstance(amount, Decimal) or not amount.is_finite():
        return False
    if not isinstance(rag_area_evidence, (list, tuple)):
        return False
    clause = _as_text(clause)
    label = _governing_amenity_label(clause)
    if label is None:
        return False
    for evidence in rag_area_evidence:
        if not isinstance(evidence, (tuple, list)) or len(evidence) not in {5, 6}:
            continue
        evidence_amount, evidence_label, _, source_title, _, *_scope = evidence
        if evidence_label != label or _to_decimal(evidence_amount) != amount or not isinstance(source_title, str):
            continue
        tokens = [t for t in _normalize_clause_text(source_title).split() if len(t) >= 4]
        # An empty distinctive-token set means the title carries nothing
        # checkable, so the (label, amount, chunk) pin stands on its own.
        # One distinctive token is enough: no LLM reproduces a 13-token document
        # title inside a numeric clause, so demanding the full string rejected
        # every true amenity area while still passing the negative cases above.
        # Attribution stays CLAUSE-local: a title in a separate sentence is a
        # separate claim and must not authorize this figure.
        if not tokens or any(token in clause for token in tokens):
            return True
    return False


def _governing_amenity_label(clause: str) -> str | None:
    """The amenity label that introduces the figure in ``clause``.

    Returns the single label when exactly one is present. When several are
    present (a comma-joined list), returns the label whose match ends closest to
    the clause's trailing figure, or None when that choice is ambiguous.
    """
    clause = _as_text(clause)
    if _AMOUNT_FOR_LABEL_RE.search(clause) is None:
        return None
    normalized = _normalize_label_text(clause)
    hits: list[tuple[int, str]] = []
    for label, pattern in _AMENITY_LABELS.items():
        found = list(pattern.finditer(normalized))
        if found:
            hits.append((found[-1].end(), label))
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0][1]
    # Several amenities on one line: the figure belongs to the label nearest to
    # its LEFT, i.e. the label whose match reaches furthest into the clause.
    nearest_end = max(end for end, _ in hits)
    winners = [label for end, label in hits if end == nearest_end]
    return winners[0] if len(winners) == 1 else None


def evidence_values(facts: list[dict]) -> list[Decimal]:
    """Normalized field values from FACT_EVIDENCE blocks."""
    out: list[Decimal] = []
    for fact in _as_dict_list(facts):
        fields = fact.get("fields")
        if not isinstance(fields, dict):
            continue
        for v in fields.values():
            d = _to_decimal(v)
            if d is not None:
                out.append(d)
    return out


def _byte_match(d: Decimal, evidence: Any) -> bool:
    """Byte-match (normalized): true when an evidence value equals d exactly.

    ``evidence`` is a set built once per call (_decimal_set), so this is a hash
    lookup; membership semantics are identical for a list, which keeps the helper
    safe for direct callers.
    """
    return d in evidence


def _citation_grounding(answer: str, sources: list[dict], known_fe_ids: list[str]) -> dict:
    """Best-effort: [fe-xxx] citations and source titles must appear in sources.

    Without chunk content here, verdict stays 'pending' when there is no span to
    check; 'pass' when every citation has a matching source.
    """
    text = _as_text(answer)
    # A bare string is one id, not an iterable of characters: the old set() over a
    # string turned every citation into a miss and reported the whole answer as
    # failing. Non-string members are dropped because they can never equal an id.
    if isinstance(known_fe_ids, str):
        known = {known_fe_ids}
    else:
        known = {item for item in (known_fe_ids or []) if isinstance(item, str)}
    verdict: dict[str, Any] = {"status": "pending", "detail": "no span to check"}
    # Capture the bare id: the brackets are markup, and comparing them against
    # `known` (which holds plain fe_ids) reported EVERY citation as missing.
    fe_cites = re.findall(r"\[(fe-\d{3})\]", text)
    if fe_cites:
        missing = [c for c in fe_cites if c not in known]
        verdict = {"status": "pass" if not missing else "fail", "fe_citations": len(fe_cites), "missing": missing}
    # Source titles — best-effort substring check. A non-string title is recorded
    # as empty rather than tested, because `in` against a non-string left operand
    # raises TypeError.
    titles = [
        source.get("title") if isinstance(source.get("title"), str) else ""
        for source in _as_dict_list(sources)
    ]
    if titles:
        matched = [t for t in titles if t and (t in text or any(w in text for w in _title_words(t)))]
        verdict["source_titles_matched"] = len(matched)
        verdict["source_titles_total"] = len(titles)
    return verdict


def _title_words(title: str) -> list[str]:
    """Distinctive title words (>= 3 chars) used for substring matching."""
    return [w for w in re.split(r"\s+", _as_text(title)) if len(w) >= 3]


def _confidence_3tier(
    numeric_pass: bool,
    sql_row_count: int,
    strong_chunks: int,
    degraded: list[str],
    has_approx: bool,
    geo_places: int = 0,
    selected_chunks: int = 0,
) -> str:
    """HIGH when grounding passes and SQL has >= 1 row or >= 2 chunks at rerank >= 0.8;

    MEDIUM with a single source, degraded, or approx; LOW when not passing.

    A pure location query has no financial numbers to ground (numeric_pass is
    False by design); its grounding is the places catalog, so geo_places >= 1
    substitutes for SQL/chunk provenance instead of forcing LOW.
    """
    rows = _as_int(sql_row_count)
    chunks = _as_int(strong_chunks)
    places = _as_int(geo_places)
    selected = _as_int(selected_chunks)
    flags = _as_str_list(degraded)
    # A pure location query has no financial numbers to ground, so numeric_pass
    # is True by default; if orphan financial numbers ARE present, they still
    # force LOW regardless of geo grounding.
    if not numeric_pass:
        return "LOW"
    if places >= 1:
        # Location answer grounded by the places catalog, not financial evidence.
        # The producer writes a prefixed flag ("geo_degraded:timeout"), so an exact
        # membership test never fired and a degraded geo lookup kept its MEDIUM.
        if any(flag.startswith("geo_degraded") for flag in flags):
            return "LOW"
        return "MEDIUM"
    # The EXISTENCE of selected evidence is a fact; the rerank SCORE is not a
    # usable proxy for it. On the live corpus scores run p50 0.185 with only 1.83%
    # at >= 0.8, so gating existence on the score pinned every ordinary RAG turn
    # at LOW and withheld answers whose chunks had in fact been selected. The
    # score now only separates HIGH from MEDIUM, which is the one job it does well.
    # ``chunks >= 1`` stays as a third existence term rather than being dropped:
    # a chunk can only reach that count by having been selected, so it is proof of
    # existence too, and a caller that reports strong_chunks without also
    # populating selected_chunk_ids must not be read as having no evidence.
    any_source = rows >= 1 or chunks >= 1 or selected >= 1
    if not any_source:
        return "LOW"
    provenance_ok = rows >= 1 or chunks >= 2
    if provenance_ok and not flags and not has_approx:
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
    low = _as_text(answer).lower()
    is_price = bool(has_approx) or bool(_PRICE_HINT_RE.search(low))
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
    text = _as_text(answer)
    low = text.lower()
    hit = [p for p in _ROBOT_PHRASES if p in low]
    dash = _EM_DASH in text or _EN_DASH in text
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
    return _as_text(text).replace(_EM_DASH, "-").replace(_EN_DASH, "-")


def normalize_answer_display(text: str) -> str:
    """Rewrite raw LaTeX in a COMPLETED answer to readable Unicode/plain math.

    Applied once to the joined answer (workflow generate step), never to streamed
    tokens, so SSE emission keeps its exact token-by-token semantics. Purely
    additive/substitutive: delimiters and known commands are rewritten, unknown
    markup is left verbatim, and no content is dropped or truncated.
    """
    out = _as_text(text)
    if not out:
        return out
    out = _LATEX_FRAC_RE.sub(r"\1/\2", out)
    out = _LATEX_TEXT_RE.sub(r"\1", out)
    out = _LATEX_ENV_RE.sub("", out)
    out = _LATEX_LR_RE.sub("", out)
    out = _LATEX_CMD_RE.sub(lambda m: _LATEX_CMD_MAP.get(m.group(1), m.group(0)), out)
    out = _LATEX_ESCAPE_RE.sub(r"\1", out)
    return _LATEX_DELIM_RE.sub("", out)


def _degraded_result(exc: BaseException) -> GuardResult:
    """Fail-closed verdict for a guard that could not complete at all.

    ``verification_unavailable`` is the explicit signal for the caller; the
    numeric verdict reads "fail" because a check that never ran has established
    nothing. That is safe rather than lossy: the workflow re-derives the blocking
    spans from its own rules, so an answer whose figures really are grounded still
    publishes, while an ungrounded figure still blocks.
    """
    return GuardResult(
        confidence="LOW",
        requires_review=True,
        verdicts={
            "verification_unavailable": True,
            "error_type": type(exc).__name__,
            "numeric_grounding": "fail",
            "orphan_numbers": [],
            "citation_grounding": {"status": "pending", "detail": "verification unavailable"},
        },
    )


def _evaluate_guard(
    answer: str,
    facts: list[dict],
    sources: list[dict],
    routing: dict | None,
    meta: dict | None,
) -> GuardResult:
    """Run the four L4 checks and derive confidence/requires_review."""
    text = _as_text(answer)
    fact_list = _as_dict_list(facts)
    source_list = _as_dict_list(sources)

    sql_row_count = _as_int(_field(meta, "sql_row_count", 0))
    strong_chunks = _as_int(_field(meta, "strong_chunks", 0))
    degraded = _as_str_list(_field(meta, "degraded", []))
    has_approx = bool(_field(meta, "has_approx", False))
    geo_places = _as_int(_field(meta, "geo_places", 0))
    # Defensive: a routing object with attribute access, or a high-stakes flag that
    # only reached meta, must escalate exactly like routing["high_stakes"].
    high_stakes = bool(_field(routing, "high_stakes", False)) or bool(
        _field(meta, "high_stakes", False)
    )

    verdicts: dict[str, Any] = {}

    # (a) numeric grounding — three authorities, strongest first: an exact fact
    # value, a labelled amenity area tied to its exact source title, then (for
    # NON-money figures only) text from a chunk that passed scope validation.
    # SQL facts remain authoritative for money; the chunk path exists because an
    # answer legitimately quotes areas and percentages from the retrieved chunks,
    # and treating those as orphans withheld correct answers from customers.
    try:
        evidence = _decimal_set(evidence_values(fact_list))
        area_evidence = _field(meta, "_rag_area_evidence", [])
        chunk_evidence = _decimal_set(_field(meta, "_chunk_numeric_evidence", []))
        binding_evidence = _field(meta, "_chunk_binding_evidence", [])
        orphan: list[Decimal] = []
        for amount, unit, start, end in _extract_amount_matches(text):
            if _byte_match(amount, evidence):
                continue
            # Computed once and shared by both weaker paths: the chunk path needs
            # the same clause to veto a figure the registry binds differently.
            clause = _clause_for_span(text, start, end)
            if _rag_area_match(amount, unit, clause, area_evidence):
                continue
            if _chunk_numeric_match(
                amount, unit, chunk_evidence, clause, area_evidence, binding_evidence
            ):
                continue
            orphan.append(amount)
    except Exception as exc:  # noqa: BLE001 — an unrun check must never read "pass"
        numeric_pass = False
        verdicts["numeric_grounding"] = "fail"
        verdicts["orphan_numbers"] = []
        verdicts["numeric_check_error"] = type(exc).__name__
        verdicts["verification_unavailable"] = True
    else:
        verdicts["numeric_grounding"] = "pass" if not orphan else "fail"
        verdicts["orphan_numbers"] = str(orphan[:10]) if orphan else []
        numeric_pass = not orphan

    # (b) citation grounding best-effort
    try:
        citation_verdict = _citation_grounding(
            text, source_list, [fact.get("fe_id") for fact in fact_list]
        )
        if not isinstance(citation_verdict, dict):
            raise TypeError("citation verdict is not a mapping")
    except Exception as exc:  # noqa: BLE001 — an unchecked citation stays pending
        citation_verdict = {"status": "pending", "detail": "verification unavailable"}
        verdicts["citation_check_error"] = type(exc).__name__
        verdicts["verification_unavailable"] = True
    verdicts["citation_grounding"] = citation_verdict
    # A citation naming an evidence id that does not exist is a VERIFIABLE
    # grounding failure (unlike the "pending" verdict when there is no span to
    # check), so it forces LOW exactly the way an orphan number does — and LOW
    # is what raises requires_review below.
    citation_fail = citation_verdict.get("status") == "fail"

    # (c) confidence 3-tier
    confidence = _confidence_3tier(
        numeric_pass and not citation_fail,
        sql_row_count,
        strong_chunks,
        degraded,
        has_approx,
        geo_places,
        len(_as_str_list(_field(meta, "selected_chunk_ids", []))),
    )

    # (d) story 4.2: contextual disclosure + robot-phrase (style checks, non-confidence).
    # Isolated because these are cosmetic: a failure here must never turn into the
    # LOW + verification_unavailable verdict that withholds a grounded answer.
    try:
        disclosure = _contextual_disclosure_verdict(text, has_approx, high_stakes)
        robot = _robot_phrase_verdict(text)
    except Exception as exc:  # noqa: BLE001 — cosmetic check, never confidence-bearing
        verdicts["style_check_error"] = type(exc).__name__
        disclosure = {"status": "pending", "style_warn": False, "disclosure_scope": "none", "missing": []}
        robot = {"status": "pending", "style_warn": False, "robot_phrases": [], "em_dash": False}
    verdicts["disclosure"] = disclosure
    verdicts["robot_phrase"] = robot

    # (e) requires_review: LOW + high_stakes keywords
    requires_review = confidence == "LOW" or high_stakes
    verdicts["high_stakes"] = high_stakes
    verdicts["confidence"] = confidence

    return GuardResult(confidence=confidence, requires_review=requires_review, verdicts=verdicts)


async def guard_output(
    answer: str,
    facts: list[dict],
    sources: list[dict],
    routing: dict | None,
    meta: dict | None = None,
) -> GuardResult:
    """Run the four L4 checks, then confidence/requires_review (never raises).

    An escaping exception is not a diagnostic here: the caller turns it into
    ``verification_failed`` and replaces the customer's answer with a canned
    string even when retrieval returned the right chunks. Each check therefore
    degrades in place, and this last-resort handler returns an explicit
    ``verification_unavailable`` verdict instead of raising. Cancellation is not
    caught — it is not an input failure and must keep propagating.

    Note for whoever reads ``output_guard_timeout`` in the audit: this coroutine
    has no await point, so ``asyncio.wait_for`` cannot preempt it — it only
    observes a result that has already been computed. The step budget bounds
    nothing here, and the flag is unreachable by construction; the guard's own
    speed is the only thing keeping the step inside it.
    """
    try:
        return _evaluate_guard(answer, facts, sources, routing, meta)
    except Exception as exc:  # noqa: BLE001 — never cost the customer the answer
        logger.exception("guard_output degraded: %s", type(exc).__name__)
        return _degraded_result(exc)
