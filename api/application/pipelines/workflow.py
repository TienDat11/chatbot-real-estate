"""LlamaIndex Workflows orchestrator for the 8-step query pipeline (AD-18).

Flow: guard -> rewrite/route -> RAG + SQL + geo legs in parallel -> rerank ->
merge (SSE: places -> sources -> facts) -> generate (buffer) -> output guard
(verify-before-delivery: ONE token frame on success, none on fail-closed) ->
audit. Every step degrades gracefully on timeout/error instead of crashing.

Cross-step data lives in ``ctx.store`` (DictState); events are routing signals
only, so the step graph stays a pure DAG.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from llama_index.core.workflow import (
    Context,
    Event,
    StartEvent,
    StopEvent,
    Workflow,
    step,
)

from api import get_cfg
from api.application.services.audit import write_audit
from api.application.services.conv_state import conv_directive, get_context
from api.application.services.fact_display import POLICY_DISPLAY
from api.application.services.generate import stream_answer
from api.application.services.image_search import (
    fetch_price_board_images,
    is_full_price_board_query,
    search_images,
)
from api.application.services.merge import Merged, merge_context
from api.application.services.output_sanitizer import sanitize_answer_text
from api.application.services.project_config import request_project_snapshot
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult, run_rag_leg
from api.application.services.sql_leg import (
    ConfirmedPriceProbe,
    SqlLegResult,
    SpecError,
    probe_confirmed_price,
    run_sql_leg,
    validate_spec,
)
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_input import guard_input, rule_screen
from api.domain.services.guard_output import GuardResult as OutputGuardResult
from api.domain.services.guard_output import (
    _chunk_binding_evidence,
    _chunk_numeric_match,
    _clause_for_span,
    _extract_amount_matches,
    _rag_area_match,
    evidence_values,
    guard_output,
    normalize_answer_display,
    sanitize_output,
)
from api.domain.services.rewrite import RoutedResult, fallback_route, rewrite_query
from api.domain.services.utils import sha256_hex
from api.domain.value_objects.constants import (
    SSE_EVENT_FACTS,
    SSE_EVENT_IMAGES,
    SSE_EVENT_PLACES,
    SSE_EVENT_SOURCES,
    SSE_EVENT_TOKEN,
)
from api.infrastructure.config.config import project_geo_center
from api.infrastructure.dependencies import get_geo, get_reranker
from api.infrastructure.ports.geo import GeoResult
from api.domain.services.unit_code import extract_unit_codes, is_unit_price_query, normalize_unit_code, unit_code_from_subject_suffix


_PRICE_IMAGE_KINDS = frozenset({"banggia", "thanh-toan"})
_PRICE_IMAGE_CUES = ("gia", "price", "vnd", "dong", "trieu", "ty", "thanh toan", "lai suat")


def _has_image_price_claim(image: dict[str, Any]) -> bool:
    if str(image.get("kind") or "").lower() in _PRICE_IMAGE_KINDS:
        return True
    text = " ".join(
        str(image.get(key) or "") for key in ("title", "caption", "alt_text", "reason")
    )
    normalized = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
    plain = "".join(char for char in normalized if not unicodedata.combining(char))
    return any(re.search(rf"\b{re.escape(cue)}\b", plain) for cue in _PRICE_IMAGE_CUES)


def _unit_subject_code(subject: object, project_key: str) -> str | None:
    if not isinstance(subject, str):
        return None
    parts = subject.split("/")
    if len(parts) != 2 or parts[0] != f"unit:{project_key}":
        return None
    return unit_code_from_subject_suffix(parts[1])


_PROJECT_NAME_OPS = ("=", "ilike")
_PROJECT_NAME_PATTERN_CHARS = ("%", "_", "\\")


def _normalize_project_name_filter(
    spec: dict | None, project_context: ProjectContext | None
) -> dict | None:
    """Strip ONE literal ``project_name`` filter that names THIS project.

    The model may name the project it was asked about; the server owns the scope.
    Only a literal value (no ``%``/``_``/``\\``, so neither SQL-pattern nor
    substring semantics) that matches exactly one entry of the trusted active
    catalogue — and that entry is the selected project — is metadata to drop.
    Everything else (two such filters, a foreign/ambiguous name, a pattern, a
    non-string value) is returned untouched, so the closed validator rejects it.
    """
    if not isinstance(spec, dict) or not isinstance(project_context, ProjectContext):
        return spec
    filters = spec.get("filters")
    if not isinstance(filters, list):
        return spec
    candidates = [
        item for item in filters if isinstance(item, dict) and item.get("field") == "project_name"
    ]
    if len(candidates) != 1:
        return spec
    candidate = candidates[0]
    if candidate.get("op") not in _PROJECT_NAME_OPS:
        return spec
    value = candidate.get("value")
    if not isinstance(value, str) or not value.strip():
        return spec
    literal = value.strip()
    if any(char in literal for char in _PROJECT_NAME_PATTERN_CHARS):
        return spec
    folded = literal.casefold()
    # Count catalogue ENTRIES, not distinct keys: a duplicated display name or a
    # name that is another project's key is ambiguous, never a match.
    matched = [
        key
        for key, display_name in project_context.active_catalogue
        if folded in (key.strip().casefold(), display_name.strip().casefold())
    ]
    if matched != [project_context.project_key]:
        return spec
    normalized = dict(spec)
    normalized["filters"] = [item for item in filters if item is not candidate]
    return normalized


# P0-04 §2D: an EXACT confirmed price is asked for explicitly. A generic
# "Giá căn CH-03?" stays a scoped facts spec so a separately authenticated,
# expressly labelled range/estimate can still be published (P0-03C).
_EXACT_PRICE_MARKERS = ("xac nhan", "chinh thuc", "gia chot", "chot gia")


def _has_exact_price_intent(query: str) -> bool:
    text = unicodedata.normalize("NFD", (query or "").lower()).replace("đ", "d")
    plain = "".join(char for char in text if not unicodedata.combining(char))
    return any(marker in plain for marker in _EXACT_PRICE_MARKERS)


def _exact_unit_price_spec(query: str, project_key: str) -> dict | None:
    """Server-built spec for one explicitly confirmed concrete-unit price.

    The subject key is the canonical ``unit:<project_key>/CH-XX`` the probe and
    the seeds use, so a typed ``CH-3`` resolves to ``CH-03`` and never to a type
    band or another unit. Multi-unit compare is not one unit's price, so it is
    left to the ordinary scoped path.
    """
    codes = list(dict.fromkeys(extract_unit_codes(query)))
    if len(codes) != 1 or not is_unit_price_query(query):
        return None
    if not _has_exact_price_intent(query):
        return None
    return {
        "source": "facts",
        "subject_type": "unit",
        "structured_path": "exact_unit_price",
        "filters": [
            {"field": "subject_key", "op": "=", "value": f"unit:{project_key}/{codes[0]}"},
            {"field": "price_vnd", "op": ">", "value": 0},
        ],
        "order_by": {"field": "price_vnd", "dir": "asc"},
        "limit": 1,
    }


def _presentation_facts(
    facts: list[dict[str, Any]], project_key: str | None, unit_codes: list[str]
) -> list[dict[str, Any]]:
    if not unit_codes:
        return facts
    if not project_key or len(unit_codes) != 1:
        return []
    requested = set(unit_codes)
    return [
        fact
        for fact in facts
        if _unit_subject_code(fact.get("subject"), project_key) in requested
    ]


def _presentation_images(
    images: list[dict[str, Any]], project_key: str | None, unit_codes: list[str]
) -> list[dict[str, Any]]:
    if not unit_codes:
        return images
    if not project_key or len(unit_codes) != 1:
        return []
    requested = set(unit_codes)
    visible: list[dict[str, Any]] = []
    for image in images:
        if image.get("project_key") != project_key or _has_image_price_claim(image):
            continue
        unit_code = image.get("unit_code")
        if not isinstance(unit_code, str) or normalize_unit_code(unit_code) not in requested:
            continue
        visible.append(image)
    return visible


# --- P0-03C: per-claim financial authorization ------------------------------
# Every money / rate / payment figure is authorized CLAIM BY CLAIM against a
# selected fact row that carries a fact id proven against the SQL leg, a
# same-project subject and policy, and current published-source + active
# campaign lineage. Structural prompt integrity is a SEPARATE check: a properly
# selected range/estimate row is not "unverified", it simply is not authority
# for a confirmed single-unit quote, and the presence of a range never grants
# authority on its own.

# §7 P0-03C limitation copy. The legacy "giá chính xác" phrasing is gone: an
# absence is only ever asserted from a completed scoped lookup.
NO_CONFIRMED_UNIT_PRICE = "Hiện chưa có giá xác nhận cho căn {unit_code} trong dự án này."
NO_CONFIRMED_PROJECT_PRICE = "Hiện chưa có giá bán được xác nhận cho dự án này."
PRICE_LOOKUP_UNCERTAIN = "Chưa thể xác nhận giá từ nguồn hiện có."

# Money and percentage spans are the only figures this module authorizes; m²
# stays on the guard's existing narrow registry-backed amenity exception.
_FIGURE_UNITS = frozenset(
    {"tỷ", "tỉ", "triệu", "ngàn", "nghìn", "đồng", "đ", "vnđ", "%"}
)

# A figure binds ONLY to the named field its OWN clause names, so a deposit,
# payment or rate amount can never be licensed by a same-valued price row. The
# estimate board keys (price_min_vnd/price_max_vnd) are the range counterpart of
# price_vnd: they license a labelled range claim, and only one.
_PRICE_FIELDS = frozenset({"price_vnd", "price_min_vnd", "price_max_vnd"})
_RATE_FIELDS = frozenset({"interest_rate_pct"})
_DEPOSIT_PCT_FIELDS = frozenset({"deposit_pct"})
_DEPOSIT_VND_FIELDS = frozenset({"required_down_payment_vnd"})
_PAYMENT_VND_FIELDS = frozenset(
    {"loan_amount_vnd", "monthly_principal_vnd", "monthly_interest_estimate_vnd"}
)

# Cues are matched on the accent-stripped form (see _fold). An unlabelled
# percentage resolves to no field at all and is therefore unsupported.
_RATE_CUE_RE = re.compile(r"(?<!\w)(?:lai suat|interest rate)(?!\w)")
_DEPOSIT_CUE_RE = re.compile(r"(?<!\w)(?:dat coc|tra truoc|down payment|deposit)(?!\w)")
_PAYMENT_CUE_RE = re.compile(
    r"(?<!\w)(?:tra gop|tra thang|thanh toan|hang thang|monthly payment"
    r"|khoan vay|so tien vay|goc vay|du no|tra no)(?!\w)"
)
# The range gate's label must cover the estimate wording sql_leg's `_facts_note`
# hands the model ("khoảng min–max (dữ liệu range)") or a correctly-cited range
# is dropped at publish time. The alternation is anchored to whole folded words
# so bare "tạm tính" (folds to "tam tinh...") is the only known collision,
# acceptable because it is still an estimate label and the exact+confirmed
# branch returns before the label is ever consulted.
_ESTIMATE_LABEL_RE = re.compile(r"(?<!\w)(?:uoc tinh|khoang|dao dong|tam)(?!\w)")
# A cue-less monetary clause is NOT a price. Failing open to _PRICE_FIELDS
# would let any loan/debt noun outside _PAYMENT_CUE_RE ("khoan vay", "du no",
# "goc vay") bind to a same-valued price_vnd row — and db/schema.sql makes
# loan_amount_vnd a pure function of price and deposit, so it always matches.
_PRICE_CUE_RE = re.compile(
    r"(?<!\w)(?:gia(?! dinh)|gia ban|gia banh|gia xac nhan|gia thue|bang gia|price)(?!\w)"
)

# Lineage a financial claim depends on. A prompt row that drops or invents any
# of these is not the row the SQL leg returned, so it authorizes nothing.
_LINEAGE_FIELDS = (
    "unit",
    "source_doc_id",
    "source_doc_status",
    "source_doc_project_key",
    "source_doc_effective_from",
    "source_doc_effective_to",
    "campaign_key",
    "campaign_project_key",
    "campaign_status",
    "campaign_effective_from",
    "campaign_effective_to",
    "campaign_source_doc_id",
    "campaign_source_doc_status",
    "campaign_source_doc_project_key",
    "campaign_source_doc_effective_from",
    "campaign_source_doc_effective_to",
)
_FACT_MATCH_FIELDS = (
    "fe_id",
    "subject",
    "policy_key",
    "quality",
    "trust_level",
    "fields",
    "range",
    "effective_from",
    "effective_to",
    *_LINEAGE_FIELDS,
)


def _fold(text: str) -> str:
    """Accent-, case- and whitespace-insensitive form for label matching."""
    stripped = "".join(
        char
        for char in unicodedata.normalize("NFD", text or "")
        if not unicodedata.combining(char)
    ).replace("đ", "d").replace("Đ", "D")
    return " ".join(stripped.casefold().split())


# Every registered policy label, longest first so a longer label is handled
# before a shorter one that is its prefix. Built from the single source
# (fact_display.POLICY_DISPLAY) so a new policy key cannot silently keep
# mis-classifying the clause that names it.
_POLICY_LABELS = tuple(
    sorted(
        {label for label in POLICY_DISPLAY.values() if label},
        key=len,
        reverse=True,
    )
)


def _as_decimal(value: Any) -> Decimal | None:
    """Numeric value of a fact field; None for bools, text and non-numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _interval_current(start: Any, end: Any, today: date) -> bool:
    """Half-open [from, to) validity at today; a null end never expires."""
    try:
        if not isinstance(start, str) or date.fromisoformat(start) > today:
            return False
    except (TypeError, ValueError):
        return False
    if end is None:
        return True
    try:
        return isinstance(end, str) and date.fromisoformat(end) > today
    except (TypeError, ValueError):
        return False


def _has_current_financial_lineage(row: dict, project_key: Any, today: date) -> bool:
    """Fact + source document + campaign + campaign document, all current.

    Intervals are checked against ``date.today()``, never ``as_of``: an
    explicitly historical lookup cannot authorize a *current* price.
    """
    if not isinstance(project_key, str) or not project_key.strip():
        return False
    return (
        _interval_current(row.get("effective_from"), row.get("effective_to"), today)
        and bool(row.get("source_doc_id"))
        and row.get("source_doc_project_key") == project_key
        and row.get("source_doc_status") == "published"
        and _interval_current(
            row.get("source_doc_effective_from"), row.get("source_doc_effective_to"), today
        )
        and bool(row.get("campaign_key"))
        and row.get("campaign_project_key") == project_key
        and row.get("campaign_status") == "active"
        and _interval_current(
            row.get("campaign_effective_from"), row.get("campaign_effective_to"), today
        )
        and bool(row.get("campaign_source_doc_id"))
        and row.get("campaign_source_doc_project_key") == project_key
        and row.get("campaign_source_doc_status") == "published"
        and _interval_current(
            row.get("campaign_source_doc_effective_from"),
            row.get("campaign_source_doc_effective_to"),
            today,
        )
    )


def _sql_confirms_fact(row: dict, sql_rows: list[dict]) -> bool:
    """The prompt row must be the leg row it was serialized from.

    A selected fe row is authority only when it carries a fact id that
    identifies exactly one original ``SqlLegResult`` row and every value a
    claim depends on is byte-identical on both sides; a fabricated or id-less
    prompt row fails here.
    """
    fact_id = row.get("fact_id")
    if fact_id is None or fact_id == "":
        return False
    matches = [
        other
        for other in (sql_rows or [])
        if isinstance(other, dict) and other.get("fact_id") == fact_id
    ]
    if len(matches) != 1:
        return False
    return all(row.get(field) == matches[0].get(field) for field in _FACT_MATCH_FIELDS)


def _fact_range_bounds(row: dict) -> tuple[Decimal, Decimal] | None:
    """Ordered positive bounds of a RANGE/estimate row, else None."""
    if row.get("quality") != "range" or row.get("trust_level") != "estimate":
        return None
    bounds = row.get("range")
    if not isinstance(bounds, dict):
        return None
    low, high = _as_decimal(bounds.get("min")), _as_decimal(bounds.get("max"))
    if low is None or high is None or not 0 < low <= high:
        return None
    return low, high


def _named_keys(
    rows: list[dict],
    text: str,
    display_field: str,
    identity_field: str,
    project_key: str | None = None,
) -> set[str]:
    """Identity values this text names, via a display label or a canonical code."""
    folded = _fold(text)
    named: set[str] = set()
    for row in rows:
        identity = row.get(identity_field)
        if identity is None:
            continue
        key = str(identity)
        label = _fold(row.get(display_field) or "")
        if label and re.search(rf"(?<!\w){re.escape(label)}(?!\w)", folded):
            named.add(key)
        code = _unit_subject_code(key, project_key) if project_key else None
        if code and re.search(rf"(?<!\w){re.escape(code)}(?!\w)", folded, re.IGNORECASE):
            named.add(key)
    return named


def _sole_policy(rows: list[dict]) -> str | None:
    policies = {str(row["policy_key"]) for row in rows if row.get("policy_key")}
    return next(iter(policies)) if len(policies) == 1 else None


def _resolve_claim_identity(
    rows: list[dict],
    clause: str,
    query_text: str,
    project_key: str,
    context: str = "",
) -> tuple[str | None, str | None]:
    """(subject, policy_key) for one claim, or (None, None) when unresolved.

    The claim's OWN clause decides: it is the only text that may bind a figure
    to a subject or a policy. The query is a fallback for a clause that names
    neither, so a generic question can still be answered per clause. A query
    naming two units is not a conflict — it is what makes the clause necessary.
    An ambiguous clause, or two candidate policies, stays unsupported rather
    than being silently resolved.

    ``context`` is the answer text PRECEDING the clause, used only when the
    clause names no subject itself. An answer groups its rows under a heading
    ("**Căn hộ Studio:**" followed by one bullet per payment method), so the
    bullet that states a price legitimately inherits the heading's unit — the
    alternative is refusing every grouped answer. A context that names two
    subjects is still ambiguous and resolves to nothing.
    """

    def _decide(named: set[str], fallback: str | None) -> str | None:
        """One name, or nothing at all. Two names is a BLOCK, never a choice.

        ``fallback`` applies only to an EMPTY clause set: a clause that names
        two subjects is ambiguous, and quietly taking the query's single
        subject would bind the figure to a unit the clause never claimed.
        """
        if len(named) > 1:
            return None
        return next(iter(named)) if named else fallback

    clause_subjects = _named_keys(rows, clause, "subject_display", "subject", project_key)
    subject = _decide(
        clause_subjects,
        _sole(_named_keys(rows, context, "subject_display", "subject", project_key))
        or _sole(_named_keys(rows, query_text, "subject_display", "subject", project_key)),
    )
    policy = _decide(
        _named_keys(rows, clause, "policy_display", "policy_key"),
        _sole(_named_keys(rows, context, "policy_display", "policy_key"))
        or _sole(_named_keys(rows, query_text, "policy_display", "policy_key"))
        or _sole_policy(rows),
    )
    return subject, policy



def _sole(named: set[str]) -> str | None:
    return next(iter(named)) if len(named) == 1 else None


def _claim_field_scope(clause: str, is_percent: bool) -> frozenset[str]:
    """The fact fields a figure may bind to, decided by its OWN clause.

    Allowlist, never fail-open: a clause with no recognised cue binds to
    NOTHING. Defaulting a bare monetary clause to _PRICE_FIELDS would license
    loan/debt figures on price authority, because ``loan_amount_vnd`` is
    derived from the price and therefore numerically equals it.

    The clause is scrubbed of every registered policy label first. A policy NAME
    carries the cue words it describes ("Phương án thanh toán sớm 95%",
    "Phương án hỗ trợ lãi suất (HTLS)"), and an answer that names the method it
    is quoting — which the prompt REQUIRES — would otherwise be classified as a
    payment or rate claim and its price figure refused. Only the label is
    removed: any cue the writer stated independently still governs.
    """
    folded = _fold(clause)
    for label in _POLICY_LABELS:
        folded = re.sub(rf"(?<!\w){re.escape(_fold(label))}(?!\w)", " ", folded)
    if _RATE_CUE_RE.search(folded):
        return _RATE_FIELDS
    if _DEPOSIT_CUE_RE.search(folded):
        return _DEPOSIT_PCT_FIELDS if is_percent else _DEPOSIT_VND_FIELDS
    if _PAYMENT_CUE_RE.search(folded):
        return frozenset() if is_percent else _PAYMENT_VND_FIELDS
    if is_percent:
        return frozenset()
    return _PRICE_FIELDS if _PRICE_CUE_RE.search(folded) else frozenset()


# A percentage's cue is classified COARSER than _claim_field_scope's field set on
# purpose. The field set answers "which SQL row may license this", and is empty
# for the payment and cue-less cases -- an empty answer that cannot be compared
# with anything. The chunk authority needs the opposite: a label for what the text
# itself says, so the chunk's clause and the answer's clause can be checked for
# agreement even when one of them names no bindable field.
_PCT_CUE_OTHER = "other"


def _pct_cue_class(clause: str) -> str:
    """Classify which policy a percentage clause names, or ``other``.

    Ordered exactly like ``_claim_field_scope`` so the two never disagree about a
    clause that names more than one cue.
    """
    folded = _fold(clause or "")
    if _RATE_CUE_RE.search(folded):
        return "rate"
    if _DEPOSIT_CUE_RE.search(folded):
        return "deposit"
    if _PAYMENT_CUE_RE.search(folded):
        return "payment"
    return _PCT_CUE_OTHER


def _pct_segment_clause(text: str, start: int, end: int) -> str:
    """The comma-bounded segment around a span, expressed as a clause.

    Commas do not end a clause, so a payment schedule written on one line is a
    single clause that names several cues at once -- "đặt cọc 5%, thanh toán 30%"
    classifies as a deposit clause for every figure in it. Rewriting commas to a
    clause break recovers the narrower segment a figure actually sits in, and
    re-uses the guard's own splitter rather than re-deriving its boundaries. The
    rewrite is one character for one, so the span offsets stay valid.
    """
    return _clause_for_span(text.replace(",", "."), start, end)


def _chunk_pct_evidence(rows: list[dict]) -> list[tuple[Decimal, str]]:
    """(percentage, cue class) pairs stated by the validated chunk texts.

    The percentage counterpart of the guard's chunk authority: a percentage is a
    dimension a validated chunk may legitimately state, so the value's presence in
    a scope-validated chunk is what makes it real. Both the figure's own clause and
    its narrower comma segment are recorded, because a one-line schedule puts
    several cues in one clause: keeping both granularities lets a faithful short
    quote match the chunk without letting an unrelated cue through.
    """
    out: list[tuple[Decimal, str]] = []
    for row in rows:
        content = row.get("content")
        if not isinstance(content, str):
            continue
        for amount, unit, start, end in _extract_amount_matches(content):
            if unit != "%":
                continue
            out.append((amount, _pct_cue_class(_clause_for_span(content, start, end))))
            segment = _pct_segment_clause(content, start, end)
            if segment:
                out.append((amount, _pct_cue_class(segment)))
    return out


def _pct_claim_bound_by_chunk(
    answer: str,
    spans: list[tuple[Decimal, str | None, int, int]],
    evidence: list[tuple[Decimal, str]],
) -> bool:
    """True when a validated chunk states EVERY percentage of this claim.

    Two conditions, and the first is the one that must never weaken: the value has
    to occur as a percentage in a chunk that passed scope validation. A rate or a
    deposit percentage that no retrieved chunk states is a fabricated financial
    commitment, so an empty or non-matching evidence list refuses it.

    The second condition closes the misattribution case where the number is real
    but the commitment is not: a figure whose own comma segment names a determinate
    policy is refused when the chunk attributes that value only to a DIFFERENT
    determinate policy. A clause can carry several commitments at once
    ("lãi suất 0%, đặt cọc 30%"), so each figure's cue is read from its own comma
    segment, not the whole clause. A cue-less segment on either side is not a
    conflict -- the module already refuses to bind a cue-less percentage to a fact
    field, and treating "no cue" as disagreement would veto faithful short quotes.
    """
    if not evidence or not spans:
        return False
    if any(unit != "%" for _value, unit, _start, _end in spans):
        return False
    for value, _unit, start, end in spans:
        # The cue is read from the figure's own comma segment, not the whole clause,
        # so one clause can carry several commitments ("lãi suất 0%, đặt cọc 30%").
        answer_class = _pct_cue_class(_pct_segment_clause(answer, start, end))
        classes = {cue for amount, cue in evidence if amount == value}
        if not classes:
            return False
        determinate = classes - {_PCT_CUE_OTHER}
        if answer_class != _PCT_CUE_OTHER and determinate and answer_class not in determinate:
            return False
    return True


def _inside_policy_label(
    text: str, span: tuple[Decimal, str | None, int, int]
) -> bool:
    """True when a figure sits INSIDE a registered policy label.

    ``som95`` is displayed as "Phương án thanh toán sớm 95%", so the label itself
    carries a percentage. Counting it as a claim makes the clause hold a percent
    span beside its price spans, which makes ``_bind_claim`` refuse the whole
    clause (one claim may not mix units) — the price band is then reported as an
    unsupported figure. The figure belongs to the method's NAME, not to the
    answer's own arithmetic.

    Matched on the ORIGINAL text, not the folded form: folding strips accents and
    collapses whitespace, so its offsets do not address the source string.
    """
    _value, _unit, start, end = span
    for label in _POLICY_LABELS:
        for hit in re.finditer(re.escape(label), text, re.IGNORECASE):
            if hit.start() <= start and end <= hit.end():
                return True
    return False


def _financial_claims(
    text: str,
) -> list[tuple[str, list[tuple[Decimal, str | None, int, int]]]]:
    """Split an answer into its own-clause money/percentage claims.

    Spans and their normalization come from the guard's own extractor, so a
    claim maps onto exactly the spans the verifier reported as orphans. Each
    span is MASKED before the clause boundary is computed: the thousands dots
    in "từ 1.980.000.000 đồng đến 2.640.000.000 đồng" are punctuation to the
    guard's clause splitter, and unmasked they tear one range into two
    half-ranges that neither binds. The mask keeps every other character, so
    cue and estimate-label matching still sees the writer's own words.
    """
    spans = [
        match
        for match in _extract_amount_matches(text or "")
        if match[1] is None or match[1] in _FIGURE_UNITS
    ]
    spans = [span for span in spans if not _inside_policy_label(text or "", span)]
    if not spans:
        return []
    masked = list(text)
    for _value, _unit, start, end in spans:
        masked[start:end] = "\x00" * (end - start)
    masked_text = "".join(masked)
    claims: list[tuple[str, list[tuple[Decimal, str | None, int, int]], bool, str]] = []
    for span in spans:
        clause = _clause_for_span(masked_text, span[2], span[3], source=text)
        # A clause may carry two kinds of figure: the price band plus the
        # deposit/rate the same policy attaches to it ("khoảng 1,98 – 2,64 tỷ
        # (vốn tự có 30%, hỗ trợ lãi suất 0% trong 18 tháng)"). One claim must be
        # all-money or all-percent, because a single fact row licenses one kind,
        # so a mixed clause is split at the change of kind. Both halves keep the
        # SAME clause text, so each is still judged by the cue the writer stated.
        #
        # Percentages split further, by the cue of their own comma segment: one
        # clause states several distinct commitments ("vốn tự có 30%, lãi suất
        # 0% trong 18 tháng"), and no single row licenses both a deposit and a
        # rate. Grouping them by the whole clause's cue left the figure whose cue
        # the clause did not name first unbound, which refused the entire answer.
        kind = span[1] == "%"
        cue = (
            _pct_cue_class(_pct_segment_clause(text, span[2], span[3]))
            if kind
            else ""
        )
        if (
            not claims
            or claims[-1][0] != clause
            or claims[-1][2] != kind
            or claims[-1][3] != cue
        ):
            claims.append((clause, [], kind, cue))
        claims[-1][1].append(span)
    return [(clause, group) for clause, group, _kind, _cue in claims]


def _policy_named_in_clause(row: dict, clause: str) -> bool:
    """True when the clause names THIS row's policy and states no other cue.

    An answer is required (prompt rule 9) to present each payment method on its
    own row, which yields the bullet form "*Phương án thanh toán sớm 95%:* 1,72
    – 2,30 tỷ". The label IS the only cue such a clause carries, and it names the
    method rather than a field, so the scope lookup returns nothing and the
    price band is refused. Binding it to the price fields is safe because the
    caller still requires the clause to name this exact row's policy and the row
    to carry the licensed field: a loan or deposit row stays refused.
    """
    label = row.get("policy_display")
    if not isinstance(label, str) or not label.strip():
        return False
    return bool(re.search(rf"(?<!\w){re.escape(_fold(label))}(?!\w)", _fold(clause)))


def _bind_claim(
    row: dict, clause: str, spans: list[tuple[Decimal, str | None, int, int]]
) -> bool:
    """True when this single row authorizes EVERY span of one claim."""
    is_percent = {span[1] == "%" for span in spans}
    if len(is_percent) != 1:
        return False
    percent = is_percent.pop()
    expected_unit = "pct" if percent else "vnd"
    if (row.get("unit") or "").casefold() != expected_unit:
        return False
    scope = _claim_field_scope(clause, percent)
    fields = row.get("fields") or {}
    if (
        not percent
        and _fact_range_bounds(row) is not None
        and not any(name in scope for name in fields)
        and (
            _ESTIMATE_LABEL_RE.search(_fold(clause))
            or _policy_named_in_clause(row, clause)
        )
    ):
        # The indicative band shares its clause with the method's other terms
        # ("khoảng 1,98 – 2,64 tỷ (vốn tự có 30%, lãi suất 0% trong 18 tháng)"),
        # so the cue scan classifies the whole clause by those terms and the
        # price bounds find no licensed field. The clause either labels the band
        # an estimate or names this row's OWN policy — the two things that make
        # a band indicative rather than settled. Safe because a rate or deposit
        # row carries no range bounds and never reaches this branch, and the
        # bounds must still equal this row's exactly (checked below).
        scope = _PRICE_FIELDS
    if not scope:
        return False
    fields = row.get("fields") or {}
    exact = row.get("quality") == "exact" and row.get("trust_level") == "confirmed"
    if exact and not percent:
        # An answer may restate one price in several units ("2.850.000.000 đồng,
        # tức 2,85 tỷ"), so several spans in one clause are fine — as long as
        # every one of them IS this row's value.
        return spans[0][0] > 0 and all(
            any(_as_decimal(fields.get(name)) == span[0] for name in scope) for span in spans
        )
    if exact and percent:
        return all(any(_as_decimal(fields.get(name)) == span[0] for name in scope) for span in spans)
    # A RANGE row authorizes bounds only under the field its own clause named,
    # so a price range can never license a same-valued deposit/payment range.
    # The estimate label may also be the row's OWN policy name: an answer that
    # presents each method as its own bullet ("*Phương án thanh toán sớm 95%:*
    # 1,72 – 2,30 tỷ") states the label on the intro line, not in the bullet, and
    # requiring it per clause refused a correct band. Naming the method is what
    # makes the bounds indicative rather than settled, and the bounds must still
    # equal the row's exactly.
    bounds = _fact_range_bounds(row)
    if (
        bounds
        and not percent
        and len(spans) == 2
        and any(name in scope for name in fields)
        and (
            _ESTIMATE_LABEL_RE.search(_fold(clause))
            or _policy_named_in_clause(row, clause)
        )
    ):
        return bounds == (spans[0][0], spans[1][0])
    return False


def _bind_clause_for_spans(
    answer: str, clause: str, spans: list[tuple[Decimal, str | None, int, int]]
) -> str:
    """The text a claim is judged by: a percentage narrows to its own segment.

    One clause states several distinct commitments ("vốn tự có 30%, hỗ trợ lãi
    suất 0% trong 18 tháng"), and ``_claim_field_scope`` takes the FIRST cue it
    finds, so the deposit figure is classified by the rate cue beside it and
    finds no licensed field. The comma segment is the text the figure's own
    commitment is written in; the chunk leg already reads it this way, so both
    legs must agree on which cue governs.
    """
    if spans and spans[0][1] == "%":
        segment = _pct_segment_clause(answer, spans[0][2], spans[0][3])
        if segment:
            return segment
    return clause


def _bind_claim_across_policy(
    rows: list[dict],
    clause: str,
    spans: list[tuple[Decimal, str | None, int, int]],
    policy: str,
) -> bool:
    """True when EVERY row of one policy authorizes the same PERCENTAGE claim.

    The project-level counterpart of ``_bind_claim``: a clause that names the
    project's policy and no unit ("dự án The Camellia đang áp dụng chính sách hỗ
    trợ lãi suất với mức 0%/năm trong 18 tháng") resolves a policy but no
    subject, so there is no unit to walk rows by — yet the commitment is stated
    for every unit of that policy.

    Percentages only. A rate or deposit term IS a policy-wide commitment, so
    "every unit of this policy carries it" is the honest reading; a price is
    per-unit evidence, and licensing one from a project-level clause is how the
    price of CH-03 would authorize a claim about CH-11. Money stays subject-bound.

    Requiring ALL candidate rows to license it is what keeps this from
    generalizing one unit's figure to the whole policy: a policy whose units
    disagree leaves the claim unbound, exactly as before. Rows that do not carry
    the licensed field (a ``term_months`` row beside an ``interest_rate_pct``
    one) are not candidates, so they cannot veto a claim they say nothing about.
    """
    if not spans or any(span[1] != "%" for span in spans):
        return False
    candidates = [
        row
        for row in rows
        if str(row.get("policy_key") or "") == policy
        and (row.get("unit") or "").casefold() == "pct"
    ]
    if not candidates:
        return False
    return all(_bind_claim(row, clause, spans) for row in candidates)


def _claim_context(
    answer: str, position: int, rows: list[dict], project_key: str
) -> str:
    """Identity text a bullet's own clause omits, or "".

    Two sources, in the order the answer states them:

    * the rest of the bullet line BEFORE the figure — where the payment method
      is named ("Phương án thanh toán sớm 95%: khoảng ");
    * the nearest PRECEDING line that names a unit — the group heading. The line
      is found by asking the row set which line names a subject, not by a
      markdown shape: answers head their groups as "**Căn hộ Studio:**",
      "1. **Căn hộ Studio** (diện tích khoảng 27,8 – 31,4 m²):" and other
      equally valid spellings, and a shape-based matcher misses the ones it was
      not written for. The scan stops at the first naming line, so an earlier
      group's heading never leaks in.
    """
    if position <= 0:
        return ""
    line_start = answer.rfind("\n", 0, position) + 1
    parts = [answer[line_start:position]]
    cursor = line_start
    while cursor > 0:
        previous_end = cursor - 1
        previous_start = answer.rfind("\n", 0, previous_end) + 1
        line = answer[previous_start:previous_end]
        if _named_keys(rows, line, "subject_display", "subject", project_key):
            parts.append(line)
            break
        cursor = previous_start
    return " ".join(parts)


def _bind_financial_claims(
    rows: list[dict],
    answer: str,
    query_text: str,
    project_key: Any,
    meta: dict | None = None,
) -> tuple[set[tuple[int, int]], int, bool]:
    """Bind every money/rate claim in the answer to one selected fact row.

    Returns ``(bound_span_keys, total_span_count, published_an_estimate)``.

    A percentage also has the chunk authority the guard already grants it: when no
    fact row licenses the claim, a value the validated chunk texts state is
    accepted under the conditions in ``_pct_claim_bound_by_chunk``. Money is NOT
    extended this way -- an exact sale price exists only as a fact row, and a chunk
    merely mentioning a figure is not evidence of it.

    ``meta`` defaults to no chunk authority rather than to the permissive reading,
    so a caller that cannot supply the validated evidence refuses percentages
    instead of licensing them.
    """
    if not isinstance(project_key, str) or not project_key.strip():
        return set(), 0, False
    pct_evidence = (meta or {}).get("_chunk_pct_evidence") or []
    bound: set[tuple[int, int]] = set()
    published_estimate = False
    total = 0
    for clause, spans in _financial_claims(answer):
        total += len(spans)
        licensed = False
        # The identity a bullet's own clause omits: the payment method named
        # earlier on its line, and the unit named by the nearest preceding
        # heading line. The whole preceding answer would name several units and
        # policies, which resolves to nothing.
        preceding = (
            _claim_context(answer, spans[0][2], rows, project_key) if spans else ""
        )
        subject, policy = _resolve_claim_identity(
            rows, clause, query_text, project_key, context=preceding
        )
        if subject is not None and policy is not None:
            # A percentage is judged by the cue of its OWN comma segment, not the
            # whole clause. One clause states several commitments ("vốn tự có 30%,
            # hỗ trợ lãi suất 0% trong 18 tháng"), and the cue scan takes the first
            # match, so the deposit figure was classified by the rate cue beside it
            # and found no licensed field. The segment is what the figure's own
            # commitment is written in; the chunk leg below already reads it this
            # way, and both must agree on which cue governs.
            bind_clause = _bind_clause_for_spans(answer, clause, spans)
            for row in rows:
                if str(row.get("subject") or "") != subject or str(row.get("policy_key") or "") != policy:
                    continue
                if not _bind_claim(row, bind_clause, spans):
                    continue
                licensed = True
                published_estimate = published_estimate or _fact_range_bounds(row) is not None
                break
        elif policy is not None:
            # A project-level clause names the policy but no unit ("dự án The
            # Camellia áp dụng chính sách hỗ trợ lãi suất với mức 0%/năm trong 18
            # tháng"), so there is no subject to walk rows by. The commitment is
            # stated for every unit of that policy, so every candidate row must
            # license it -- see _bind_claim_across_policy.
            licensed = _bind_claim_across_policy(
                rows, _bind_clause_for_spans(answer, clause, spans), spans, policy
            )
        # The chunk leg is a FALLBACK, never a replacement: a claim the fact leg
        # already licensed keeps exactly the authority it had before this change,
        # so no existing SQL verdict moves.
        if not licensed:
            licensed = _pct_claim_bound_by_chunk(answer, spans, pct_evidence)
        if licensed:
            bound.update((span[2], span[3]) for span in spans)
    return bound, total, published_estimate


# A sentence ends at terminal punctuation followed by whitespace, or at a line
# break. The lookbehind keeps the separator with the sentence it closes, and the
# whitespace requirement is what stops a thousands dot ("1.980.000") from
# splitting a figure in half.
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _drop_ungrounded_sentences(
    answer: str,
    rows: list[dict],
    query_text: str,
    project_key: str,
    meta: dict | None,
) -> str | None:
    """The answer without its ungrounded sentences, or None when nothing is droppable.

    A draft can be grounded everywhere except one closing paragraph: the prompt
    asks for a short benefit summary, and the model writes it by combining bounds
    from several payment methods ("từ 1,72 tỷ đến 2,64 tỷ") or by naming a method
    it abbreviates past recognition. No single fact row authorizes such a figure,
    so the claim is refused -- and refusing the CLAIM used to delete the whole
    answer, including the per-method bands that bound perfectly.

    Only sentences carrying an ungrounded figure are removed, and only when the
    remainder re-binds clean: the candidate is re-checked against the same rows
    before it is offered, so this can never launder an unbound figure. A repair
    that would leave no figure at all returns None, which keeps the existing
    canned copy for a draft that was ungrounded from the start.

    Boundaries are located on the ORIGINAL string and the kept segments are
    re-joined verbatim, so every character outside a dropped sentence survives
    byte for byte -- a rewritten string could drop the qualification that made
    another sentence publishable.
    """
    bound, total, _ = _bind_financial_claims(rows, answer, query_text, project_key, meta)
    if total == len(bound):
        return None
    cuts = [0] + [match.end() for match in _SENTENCE_BREAK_RE.finditer(answer)] + [len(answer)]
    drop: set[int] = set()
    for _clause, spans in _financial_claims(answer):
        for span in spans:
            if (span[2], span[3]) in bound:
                continue
            for index in range(len(cuts) - 1):
                if cuts[index] <= span[2] < cuts[index + 1]:
                    drop.add(index)
                    break
    if not drop:
        return None
    repaired = _strip_dangling_table_headers(
        "".join(
            answer[cuts[index] : cuts[index + 1]]
            for index in range(len(cuts) - 1)
            if index not in drop
        ).strip()
    )
    if not repaired:
        return None
    repaired_bound, repaired_total, _ = _bind_financial_claims(
        rows, repaired, query_text, project_key, meta
    )
    if repaired_total == 0 or repaired_total > len(repaired_bound):
        return None
    return repaired


_TABLE_HEADER_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_LINE_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _strip_dangling_table_headers(text: str) -> str:
    """Remove a markdown table header left with no data row.

    ``_drop_ungrounded_sentences`` removes the LINES carrying an unbound figure,
    and a table's data row is one such line. When every data row of a table is
    dropped, its header and separator survive alone — a malformed table the
    reader sees as an empty grid. A header is only meaningful with a row under
    it, so the pair goes too.
    """
    lines = text.split("\n")
    keep = [True] * len(lines)
    for index in range(len(lines) - 1):
        if not _TABLE_HEADER_LINE_RE.match(lines[index]):
            continue
        if not _TABLE_SEPARATOR_LINE_RE.match(lines[index + 1]):
            continue
        following = next(
            (line for line in lines[index + 2 :] if line.strip()), ""
        )
        if not following.lstrip().startswith("|"):
            keep[index] = keep[index + 1] = False
    return "\n".join(line for line, alive in zip(lines, keep) if alive).strip()


def _verifier_orphan_spans(
    answer: str, facts: list[dict], meta: dict | None
) -> list[tuple[int, int]]:
    """Answer spans the verifier could not ground, recomputed from its own rules.

    The aggregate ``numeric_grounding`` verdict and its ``orphan_numbers`` string
    stay verbatim in the audit; the BLOCK/WARN decision is taken per span, so it
    never parses that display text.

    The chunk authority is replayed here because the verifier grants it: a
    recomputation that stopped at the fact values would re-orphan exactly the
    non-money figure the verifier just authorized, and the final recheck below
    would then refuse a correct answer the first check had already accepted.
    The figure's own clause is hoisted and shared with both authority calls
    because the chunk path vetoes a labelled value that contradicts the
    registry: passing no clause silently disables that authority, which would
    make this recomputation disagree with the verdict it exists to replay.
    """
    evidence = evidence_values(facts or [])
    area_evidence = (meta or {}).get("_rag_area_evidence", [])
    chunk_evidence = (meta or {}).get("_chunk_numeric_evidence") or []
    binding_evidence = (meta or {}).get("_chunk_binding_evidence") or []
    orphans: list[tuple[int, int]] = []
    for amount, unit, start, end in _extract_amount_matches(answer or ""):
        if any(amount == value for value in evidence):
            continue
        # A figure INSIDE a registered policy label is the method's NAME, not the
        # answer's own arithmetic ("Phương án thanh toán sớm 95%"). The claim
        # binder already excludes it, so counting it here made the two gates
        # disagree: every method-labelled answer carried an orphan the binder
        # could never bind, and the publish gate refused it as ungrounded.
        if _inside_policy_label(answer or "", (amount, unit, start, end)):
            continue
        clause = _clause_for_span(answer, start, end)
        if _rag_area_match(amount, unit, clause, area_evidence):
            continue
        if _chunk_numeric_match(
            amount, unit, chunk_evidence, clause, area_evidence, binding_evidence
        ):
            continue
        orphans.append((start, end))
    return orphans


def _outage_figures_grounded(answer: str, facts: list[dict], meta: dict | None) -> bool:
    """Structural figure check for a turn whose verifier never answered.

    When verification is unavailable there is no ``numeric_grounding`` verdict to
    consult, so the answer's figures have to be checked here or not at all.
    Every figure must be authorized by a value in the observed FACT evidence, by
    a labelled amenity area, or -- for a figure that is not money -- by a value
    stated in the scope-validated chunk texts.

    This mirrors the verifier's own authority set exactly, and that is the point:
    the question is "would the verifier have passed this answer?", so the outage
    path can never authorize a figure the normal path would have refused. Money
    is excluded from chunk authority by ``_chunk_numeric_match`` itself, which is
    why the money rule is reused rather than restated here -- an outage must not
    become the one turn where an unbacked price reaches a customer.

    The clause is hoisted and passed to both authority calls because the chunk
    path vetoes a figure whose clause names a subject the chunk or registry binds
    to a different value, and it declines authority outright when the clause is
    missing. The binding evidence is passed for the same reason: this check has
    to run the verifier's FULL authority set, or an outage turn would authorize a
    misattributed figure that the ordinary turn refuses.
    """
    evidence = evidence_values(facts or [])
    area_evidence = (meta or {}).get("_rag_area_evidence", [])
    chunk_evidence = (meta or {}).get("_chunk_numeric_evidence") or []
    binding_evidence = (meta or {}).get("_chunk_binding_evidence") or []
    for amount, unit, start, end in _extract_amount_matches(answer or ""):
        if any(amount == value for value in evidence):
            continue
        clause = _clause_for_span(answer, start, end)
        if _rag_area_match(amount, unit, clause, area_evidence):
            continue
        if _chunk_numeric_match(
            amount, unit, chunk_evidence, clause, area_evidence, binding_evidence
        ):
            continue
        return False
    return True


def _chunk_numeric_evidence(rows: list[dict]) -> list[Decimal]:
    """Every normalized figure the retrieved chunk TEXTS actually state.

    The verifier's only numeric evidence source is the SQL/fact projection, so a
    figure the answer quotes faithfully from a retrieved chunk has nothing to be
    checked against and is reported as an orphan -- a grounding failure that
    refuses a correct, sourced answer. The chunk text is the missing source, and
    the caller passes rows that ``_prompt_evidence_integrity`` has already
    validated for scope, status and effective dates, so they carry authority; an
    unvalidated chunk must never reach here.

    Extraction reuses the verifier's own ``_extract_amount_matches`` so the
    values collected here are the very ones it later compares against. A second,
    independent number parser would drift from the orphan check and silently
    stop authorizing the figures it exists to authorize.
    """
    values: list[Decimal] = []
    for row in rows or ():
        content = row.get("content") if isinstance(row, dict) else None
        if not isinstance(content, str):
            continue
        values.extend(
            amount for amount, _unit, _start, _end in _extract_amount_matches(content)
        )
    return values


# Every reason the pipeline may substitute a canned copy for the drafted answer.
# The set is closed on purpose. The owner invariant is "selected chunks + a
# resolved scope + intact evidence integrity => an answer ships", and a turn in
# that state must publish with NO reason at all; so a value appearing here is a
# claim that one specific evidence-side fact was true, and never a place to
# record a vague "something went wrong".
FALLBACK_REASONS = frozenset({
    "no_evidence",
    "unresolved_scope",
    "cross_project_evidence",
    "integrity_failed",
    "numeric_ungrounded",
    "citation_ungrounded",
    "unsupported_figure",
    "price_unanswered",
    "verification_unavailable",
    "visible_bound_dropped_qualification",
    "final_recheck_ungrounded",
})


def _canned_fallback_reason(
    *,
    grounding_block: bool,
    verification_failed: bool,
    outage_figure_unbacked: bool,
    unresolved_scope: bool,
    cross_project_evidence: bool,
    integrity_failed: bool,
    numeric_ungrounded: bool,
    citation_ungrounded: bool,
    unsupported_figure: bool,
) -> str:
    """Name the single evidence-side fact that forced a canned copy.

    Precedence follows the copy rule itself. ``grounding_block`` is what selects
    the grounding copy, so inside it only a grounding axis can be the reason --
    the scope and integrity axes are already excluded by the time that flag is
    formed, and the residual case is a draft that asserted facts while no
    selected evidence supported them.

    Outside it the copy is the verification fallback, which exists for an
    unavailable verifier; the remaining values then describe why no publishable
    evidence was established at all.
    """
    if grounding_block:
        if numeric_ungrounded:
            return "numeric_ungrounded"
        if citation_ungrounded:
            return "citation_ungrounded"
        if unsupported_figure:
            return "unsupported_figure"
        return "no_evidence"
    if verification_failed:
        # An outage that ALSO failed the structural figure check is named for the
        # figure: that check is what decided the turn, and reporting the outage
        # alone would hide an unbacked number behind a coincidence.
        return "numeric_ungrounded" if outage_figure_unbacked else "verification_unavailable"
    if unresolved_scope:
        return "unresolved_scope"
    if cross_project_evidence:
        return "cross_project_evidence"
    if integrity_failed:
        return "integrity_failed"
    return "no_evidence"


logger = logging.getLogger("api.workflow")

# Per-step budgets (asyncio.wait_for). Measured warm latencies (scripts/timing
# probe): guard 0.1ms, rewrite <=9.3s (single call), rag <=5.9s, sql ~0.15s,
# nl2sql ~8s, rerank 0.3-0.5s, geo 0.5ms, output_guard 0.2-4ms, images ~0.3s.
# Each value keeps >=1.3x headroom over the observed happy-path time so normal
# requests are never cut, while still bounding stuck calls so the workflow
# degrades to its fallback instead of parking. rag 60.0: warm rag settles at
# 5.9-15s, but a cold LightRAG init adds ~25s of one-time graph/vector setup, so
# the previous 25.0 sat exactly on the observed 28-40s cold path and cut it into
# rag_timeout -> the customer saw the fail-closed copy on the first turn after
# every scale-out. 60.0 covers cold init while still bounding a stuck call.
# nl2sql 11.0: measured happy-path runs ~8s, so 11.0 leaves >=1.3x headroom (8.0
# sat the budget and timed out real requests). sql 3.0: run_sql_leg runs two sequential with_rls_identity(timeout_s=1.5)
# blocks, so the outer budget must cover their combined 3.0s so the inner
# statement_timeout fires before the workflow cuts. geo 3.0: covers the
# google_places HTTP TIMEOUT_S of 2.5s.
STEP_TIMEOUTS = {
    "guard": 1.0,
    "rewrite": 12.0,
    "rag": 60.0,
    "sql": 3.0,
    "sql_nl2sql": 11.0,
    "rerank": 3.0,
    "geo": 3.0,
    "output_guard": 1.5,
}

# Event callback: accepts async or sync callables (workflow awaits coroutines).
EventCallback = Callable[[str, dict], Awaitable[None] | None]
# Deterministic safe-copy for unavailable verification, missing evidence, or
# evidence that cannot be tied to the resolved project scope.
VERIFICATION_FALLBACK_MESSAGE = (
    "Tôi chưa thể xác minh câu trả lời này từ các nguồn hiện có. "
    "Vui lòng thử lại sau."
)
LOW_GROUNDING_MESSAGE = (
    "Tôi chưa thể xác nhận số liệu hoặc trích dẫn trong câu trả lời từ nguồn "
    "đúng dự án. Vui lòng cho tôi biết nội dung cần kiểm tra để hỗ trợ chính xác hơn."
)

# P0-05: the floor for a per-turn visible budget — the LONGEST STATIC copy this
# module can substitute for an answer it refuses to publish, so a smaller budget
# is rejected in `guard` before any publication rather than clipping a safety
# sentence into a mutilated one. Deliberately a static max: the per-unit price
# copy near output_guard is joined dynamically and can be longer. That copy is
# emitted WHOLE (never truncated) and may exceed a tiny budget; the GA watchdog
# then refuses the turn before persisting, which is the correct outcome.
MIN_VISIBLE_CHARS = max(
    len(LOW_GROUNDING_MESSAGE),
    len(VERIFICATION_FALLBACK_MESSAGE),
    len(PRICE_LOOKUP_UNCERTAIN),
)



class QueryRejected(Exception):
    """L1 rejection — main maps this to HTTP 400 + audit."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"rejected: {reason}")



def parse_as_of(value: str | None) -> date | None:
    """Parse 'YYYY-MM-DD' to a date; None/invalid -> None (means today)."""
    if not value:
        return None
    if isinstance(value, date):
        return value.date() if isinstance(value, datetime) else value
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        logger.warning("as_of invalid (%r) — defaulting to today", value)
        return None


# --- Workflow events (signals only) -------------------------------------------
class GuardedEv(Event):
    """L1 passed — carry the cleaned query to the router."""

    clean: str = ""


class RagRequestEv(Event):
    pass


class SqlRequestEv(Event):
    pass


class GeoRequestEv(Event):
    pass


class RagDoneEv(Event):
    pass


class SqlDoneEv(Event):
    pass


class GeoDoneEv(Event):
    pass


class MergedEv(Event):
    pass


class GeneratedEv(Event):
    pass


class RagQueryWorkflow(Workflow):
    """LlamaIndex Workflows re-implementation of the 8-step pipeline (AD-18)."""

    def __init__(self, timeout: float = 180.0, on_event: EventCallback | None = None):
        super().__init__(timeout=timeout)
        self.on_event: EventCallback = on_event or (lambda event, data: None)

    # --- shared-state helpers --------------------------------------------------
    async def _emit(self, event: str, data: dict) -> None:
        res = self.on_event(event, data)
        if inspect.isawaitable(res):
            await res

    async def _store_get(self, ctx: Context, key: str):
        """Read one store key, returning None when the step runs standalone.

        The guard step seeds most keys, but steps are also invoked directly in
        tests with a partial store (e.g. wf.sql_leg(ctx, SqlRequestEv())); a
        missing key must read as absent. The state store documents ValueError
        as its missing-path error, so only that type is treated as "absent" —
        a genuine store failure (serialization, IO) must surface loudly (M7).
        """
        try:
            return await ctx.store.get(key)
        except ValueError:  # "Path not found in state" — the documented miss
            return None

    async def _flag(self, ctx: Context, *flags: str) -> None:
        """Append degradation flags (dedup) to shared state, atomically."""
        async with ctx.store.edit_state() as state:
            degraded = list(state.get("degraded", []) or [])
            for f in flags:
                if f and f not in degraded:
                    degraded.append(f)
            state["degraded"] = degraded

    async def _write_audit(self, ctx: Context) -> None:
        """Audit is append-only and never fails the pipeline."""
        try:
            await write_audit(await ctx.store.get("audit"))
        except Exception:  # noqa: BLE001 — audit failure never crashes
            logger.exception("audit write failed (ignored)")

    # --- steps -----------------------------------------------------------------
    @step()
    async def guard(self, ctx: Context, ev: StartEvent) -> GuardedEv:
        t0 = time.perf_counter()
        trace_id = "t-" + uuid.uuid4().hex[:10]
        session_id = getattr(ev, "session_id", None)
        await ctx.store.set("trace_id", trace_id)
        await ctx.store.set("t0", t0)
        await ctx.store.set("query", ev.query)
        await ctx.store.set("session_id", session_id)
        project_context = getattr(ev, "project_context", None)
        if not isinstance(project_context, ProjectContext):
            raise ValueError("project_context is required")
        await ctx.store.set("project_context", project_context)
        await ctx.store.set("device_id", getattr(ev, "device_id", None))
        await ctx.store.set("as_of_date", parse_as_of(getattr(ev, "as_of", None)))
        await ctx.store.set("degraded", [])
        # Screen every user history turn with the same rule set as the query (L1);
        # stored history is re-embedded verbatim into rewrite/generate prompts, so
        # an injection smuggled via history must not reach the model.
        history = getattr(ev, "history", None) or []
        for turn in history:
            if turn.get("role") == "user":
                reason = rule_screen(turn["content"])
                if reason:
                    raise QueryRejected(f"L1 history: {reason}")
        await ctx.store.set("history", history)
        await ctx.store.set(
            "audit",
            {"trace_id": trace_id, "session_id": session_id, "query": ev.query, "latency_ms": None},
        )
        # P0-05: the per-turn visible budget is TRANSPORT presentation, not
        # identity. It is stored once, beside (never inside) the immutable
        # ProjectContext, and read only by the finalization block in
        # output_guard. A budget that cannot hold a complete safe sentence is
        # refused here, BEFORE anything is published — the single validator
        # also guards the GA core's request input.
        visible_limit = getattr(ev, "visible_limit", None)
        from api.application.services.global_assistant_service import (  # noqa: PLC0415
            _validate_visible_limit,
        )

        try:
            _validate_visible_limit(visible_limit)
        except ValueError:
            audit = await ctx.store.get("audit")
            audit["guard_verdicts"] = {
                "L1": "reject", "reason": "invalid_visible_limit",
            }
            await ctx.store.set("audit", audit)
            await self._write_audit(ctx)
            raise QueryRejected(
                f"visible_limit phải là số nguyên từ {MIN_VISIBLE_CHARS} trở lên"
            ) from None
        await ctx.store.set("visible_limit", visible_limit)

        try:
            guard = await asyncio.wait_for(guard_input(ev.query), timeout=STEP_TIMEOUTS["guard"])
        except asyncio.TimeoutError:
            guard = InputGuardResult(clean=ev.query, degraded=True)
            await self._flag(ctx, "guard_timeout")
        if guard.rejected:
            audit = await ctx.store.get("audit")
            audit["guard_verdicts"] = {"L1": "reject", "reason": guard.reason}
            await ctx.store.set("audit", audit)
            await self._write_audit(ctx)
            raise QueryRejected(guard.reason or "L1 rejected")
        if guard.degraded:
            await self._flag(ctx, "guard_rule_only")
        await ctx.store.set("guard", guard)
        return GuardedEv(clean=guard.clean)

    @step()
    async def route(
        self, ctx: Context, ev: GuardedEv
    ) -> RagRequestEv | SqlRequestEv | GeoRequestEv | None:
        as_of = await ctx.store.get("as_of_date")
        as_of_iso = as_of.isoformat() if as_of else None
        try:
            routed = await asyncio.wait_for(
                rewrite_query(ev.clean, await ctx.store.get("history"), as_of_iso),
                timeout=STEP_TIMEOUTS["rewrite"],
            )
        except asyncio.TimeoutError:
            routed = fallback_route(ev.clean, as_of_iso, "rewrite_timeout")
            await self._flag(ctx, "rewrite_timeout")
        except Exception as exc:  # noqa: BLE001 — router failure falls back to rag-only
            routed = fallback_route(ev.clean, as_of_iso, f"rewrite_error:{exc}")
            await self._flag(ctx, "rewrite_error")

        # P0-04: interpret the model's SQL metadata HERE, the one seam that holds
        # the trusted catalogue and the selected key. A spec the closed validator
        # rejects never reaches the SQL leg, so no connection is acquired.
        if routed.routing.get("structured_path") == "spec" and isinstance(routed.sql_spec, dict):
            project_context: ProjectContext | None = await ctx.store.get("project_context")
            spec = _normalize_project_name_filter(routed.sql_spec, project_context)
            exact_spec = (
                _exact_unit_price_spec(ev.clean, project_context.project_key)
                if project_context
                else None
            )
            if exact_spec is not None:
                spec = exact_spec
            try:
                routed.sql_spec = validate_spec(spec)
            except SpecError as exc:
                # The rejected spec stays in the audit; only the route is closed.
                await self._flag(ctx, "spec_invalid")
                routed.routing = {**routed.routing, "needs_sql": False}
                logger.warning("sql spec bị từ chối trước khi build: %s", exc)
        await self._flag(ctx, *routed.degraded)
        await ctx.store.set("routed", routed)

        audit = await ctx.store.get("audit")
        audit.update(
            rewritten_query=routed.rewritten,
            routing=routed.routing,
            structured_path=routed.routing.get("structured_path"),
            sql_spec=routed.sql_spec or None,
        )
        await ctx.store.set("audit", audit)

        # Fan out all three legs. Each leg self-gates on its routing flag, so
        # merge always collects the same three done events (no conditional join).
        ctx.send_event(RagRequestEv())
        ctx.send_event(SqlRequestEv())
        ctx.send_event(GeoRequestEv())
        return None

    @step()
    async def rag_leg(self, ctx: Context, ev: RagRequestEv) -> RagDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_rag", True):
            await ctx.store.set("rag_result", RagLegResult([], degraded=False))
            return RagDoneEv()
        project_key = (await ctx.store.get("project_context")).project_key
        try:
            result = await asyncio.wait_for(
                run_rag_leg(
                    routed.rewritten,
                    routed.hl_keywords,
                    routed.ll_keywords,
                    await ctx.store.get("as_of_date"),
                    project_key,
                ),
                timeout=STEP_TIMEOUTS["rag"],
            )
        except asyncio.TimeoutError:
            result = RagLegResult([], degraded=True, error="timeout")
            await self._flag(ctx, "rag_timeout")
        except Exception as exc:  # noqa: BLE001 — leg failure degrades, never crashes
            result = RagLegResult([], degraded=True, error=str(exc))
            await self._flag(ctx, f"rag_error:{exc}")
        if result.degraded:
            # Preserve the leg's auditable reason codes (for example,
            # ``aquery_timeout``) instead of collapsing them into an opaque
            # error string. Keep the legacy marker only when no structured
            # reason was supplied by the leg.
            await self._flag(ctx, *result.degraded_reasons)
            if not result.degraded_reasons:
                await self._flag(ctx, f"rag_degraded:{result.error or ''}")
        await ctx.store.set("rag_result", result)
        return RagDoneEv()

    @step()
    async def sql_leg(self, ctx: Context, ev: SqlRequestEv) -> SqlDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_sql", False):
            await ctx.store.set("sql_result", SqlLegResult([], {"mode": "none"}, degraded=False))
            return SqlDoneEv()
        # Shared corpus (FR-25 revision): structured facts ARE project data,
        # so a training turn runs the SQL leg against its real context project
        # like any customer turn. The marker key must never reach the scope
        # filter (facts are tagged with real project keys), so an unscoped
        # training turn skips the leg loudly instead of silently reading wrong.
        project_key = (await ctx.store.get("project_context")).project_key
        guard: InputGuardResult = await ctx.store.get("guard")
        spec = routed.sql_spec or {}
        if routed.routing.get("structured_path") == "nl2sql":
            spec = dict(spec)
            spec["structured_path"] = "nl2sql"
        elif routed.routing.get("structured_path") == "affordability":
            spec = dict(spec)
            spec["structured_path"] = "affordability"
        timeout = (
            STEP_TIMEOUTS["sql_nl2sql"]
            if routed.routing.get("structured_path") == "nl2sql"
            else STEP_TIMEOUTS["sql"]
        )
        try:
            result = await asyncio.wait_for(
                run_sql_leg(
                    spec,
                    await ctx.store.get("as_of_date"),
                    guard.clean,
                    project_key,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            result = SqlLegResult([], {"mode": "spec", "error": "timeout"}, degraded=True)
            await self._flag(ctx, "sql_timeout")
        except Exception as exc:  # noqa: BLE001 — leg failure degrades, never crashes
            result = SqlLegResult([], {"mode": "spec", "error": str(exc)}, degraded=True)
            await self._flag(ctx, f"sql_error:{exc}")
        if result.degraded:
            await self._flag(ctx, f"sql_degraded:{result.meta.get('error') or ''}")
        await ctx.store.set("sql_result", result)
        return SqlDoneEv()

    @step()
    async def geo_leg(self, ctx: Context, ev: GeoRequestEv) -> GeoDoneEv:
        routed: RoutedResult = await ctx.store.get("routed")
        if not routed.routing.get("needs_geo", False):
            await ctx.store.set("geo_result", GeoResult([], degraded=False))
            return GeoDoneEv()
        # Shared corpus (FR-25 revision): amenities belong to the project, so
        # a training turn queries geo around its real context project centre
        # (the request-bound registry snapshot carries those coords). Without
        # a resolved context project there is no centre to search around —
        # complete empty rather than a default project (fail-closed parity
        # with the rag/sql legs; the step still runs so the merge join stays
        # intact).
        project_key = (await ctx.store.get("project_context")).project_key
        # Geo center is per-project (story 8.2/10.2): the per-request registry
        # record wins; Settings defaults remain the legacy fallback.
        # when the record carries no coordinates. Direct workflow runs outside
        # the request-bound snapshot (eval/tests) keep the legacy sync read.
        # A resolved non-Camellia project with no verified coords must NEVER
        # borrow Camellia's default center -- omit the lookup (empty, degraded)
        # so no geo claim can be made for the wrong project. Camellia's own
        # legacy/default behavior is unchanged.
        record = request_project_snapshot()
        center: tuple[float, float] | None = None
        if record is not None:
            center = record.geo_center
            if center is None and project_key == "camellia":
                center = (
                    get_cfg("geo_center_lat", 16.1052),
                    get_cfg("geo_center_lng", 108.2558),
                )
        else:
            center = project_geo_center(project_key)
            if project_key != "camellia" and center == (
                get_cfg("geo_center_lat", 16.1052),
                get_cfg("geo_center_lng", 108.2558),
            ):
                center = None
        if center is None:
            await self._flag(ctx, "geo_no_center")
            await ctx.store.set("geo_result", GeoResult([], degraded=True, error="no_center"))
            return GeoDoneEv()
        center_lat, center_lng = center
        try:
            result = await asyncio.wait_for(
                get_geo().places_around(
                    center_lat,
                    center_lng,
                    get_cfg("geo_radius_m", 10000),
                ),
                timeout=STEP_TIMEOUTS["geo"],
            )
        except asyncio.TimeoutError:
            result = GeoResult([], degraded=True, error="timeout")
            await self._flag(ctx, "geo_timeout")
        except Exception as exc:  # noqa: BLE001 — geo failure degrades, never crashes
            result = GeoResult([], degraded=True, error=str(exc))
            await self._flag(ctx, f"geo_error:{exc}")
        if result.degraded:
            await self._flag(ctx, f"geo_degraded:{result.error or ''}")
        await ctx.store.set("geo_result", result)
        return GeoDoneEv()

    @step()
    async def merge(self, ctx: Context, ev: RagDoneEv | SqlDoneEv | GeoDoneEv) -> MergedEv:
        done = ctx.collect_events(ev, [RagDoneEv, SqlDoneEv, GeoDoneEv])
        if done is None:
            return None
        guard: InputGuardResult = await ctx.store.get("guard")
        routed: RoutedResult = await ctx.store.get("routed")
        rag_result: RagLegResult = await ctx.store.get("rag_result")
        sql_result: SqlLegResult = await ctx.store.get("sql_result")
        geo_result = await ctx.store.get("geo_result")
        as_of = await ctx.store.get("as_of_date")

        # App-side rerank is the single score source for confidence.
        chunks = rag_result.chunks
        try:
            chunks = await asyncio.wait_for(
                get_reranker().rerank(routed.rewritten, chunks), timeout=STEP_TIMEOUTS["rerank"]
            )
        except asyncio.TimeoutError:
            await self._flag(ctx, "rerank_timeout")
        except Exception as exc:  # noqa: BLE001
            await self._flag(ctx, f"rerank_error:{exc}")
        if any(c.get("_rerank_degraded") or c.get("_rerank_off") for c in chunks):
            await self._flag(ctx, "rerank_degraded")
        await ctx.store.set("reranked_chunks", chunks)

        # Resolve the REAL data scope once (FR-25 revision): the marker stays
        # in meta.project_key as the prompt-profile signal (generate/
        # select_answer_tier key off it), but EVERY data consumer below —
        # merge_context placeholder hydration, image enrichment, and the
        # estimates fallback via meta.retrieval_project_key — must read the
        # resolved context project, never '_training' (which matches no facts,
        # images, or estimates and silently strips training answers of their
        # structured data). Customer mode resolves byte-identically.
        session_id = await ctx.store.get("session_id")
        device_id = await self._store_get(ctx, "device_id")
        project_context: ProjectContext = await ctx.store.get("project_context")
        project_key = project_context.project_key
        training = project_context.answer_mode == "training"
        retrieval_scope = project_key

        merged: Merged = await merge_context(
            guard.clean,
            chunks,
            sql_result.rows,
            as_of,
            retrieval_scope,
        )
        merged.meta["_fact_evidence_source"] = sql_result.meta.get("source")
        merged.meta["_fact_evidence"] = sql_result.rows

        # Illustrative image enrichment is best-effort: search_images never raises,
        # so a degraded/empty result only omits images, never the pipeline.
        # Project scoping (story 10.4 / M6): the project predicate rides in the
        # search SQL itself; the unscoped legacy call is kept for project-less
        # runs (eval, direct workflow tests) whose search seam is single-arg.
        # Full price-board intent: the user asked for EVERY unit type's board, so
        # a deterministic fetch replaces semantic top_k=4 (which truncates the
        # multi-page set exactly when completeness matters).
        # Shared corpus (FR-25 revision): project imagery IS project data, so a
        # training turn gets the same scoped enrichment as a customer turn;
        # only the sales persona/CTA directive stays suppressed for the lookup
        # profile, and an unscoped training turn reads no images at all
        # (project isolation is absolute — `not retrieval_scope` also fails
        # closed on an empty-string context, never falling to the unscoped
        # legacy single-arg call).
        if training and not retrieval_scope:
            images = []
        elif retrieval_scope and is_full_price_board_query(routed.rewritten):
            images = await fetch_price_board_images(retrieval_scope)
        elif retrieval_scope:
            images = await search_images(routed.rewritten, project_key=retrieval_scope)
        else:
            images = await search_images(routed.rewritten)
        presentation_unit_codes = extract_unit_codes(guard.clean)
        presentation_facts = _presentation_facts(
            merged.facts, retrieval_scope, presentation_unit_codes
        )
        presentation_images = _presentation_images(
            images, retrieval_scope, presentation_unit_codes
        )
        await ctx.store.set("presentation_facts", presentation_facts)
        await ctx.store.set("presentation_images", presentation_images)
        await ctx.store.set("images", images)
        conv_dir = None
        conv_state_str = None
        if not training and session_id:
            ctx_conv = get_context(session_id, device_id)
            conv_dir = conv_directive(ctx_conv.state, project_key)
            conv_state_str = ctx_conv.state
        # The strong-chunk bar belongs to the active rerank model, not to this
        # module: reading it from config keeps a rerank swap a configuration
        # change instead of a code change (see rerank_strong_threshold).
        strong_threshold = float(get_cfg("rerank_strong_threshold", 0.5))
        merged.meta.update(
            query=guard.clean,
            rewritten=routed.rewritten,
            as_of=as_of.isoformat() if as_of else None,
            project_key=project_key,
            retrieval_project_key=retrieval_scope,
            degraded=await ctx.store.get("degraded"),
            sql_row_count=len(sql_result.rows),
            has_approx=any(
                e.get("quality") in ("range", "approx") or e.get("trust_level") == "estimate"
                for e in sql_result.rows
            ),
            strong_chunks=sum(
                1
                for chunk_id in (merged.meta.get("selected_chunk_ids") or ())
                if float(
                    next((c for c in chunks if c.get("id") == chunk_id), {}).get("score", 0.0)
                ) >= strong_threshold
            ),
            answer_mode=project_context.answer_mode,
            conversation_directive=conv_dir,
            conv_state=conv_state_str,  # story 4.6: answer tier selector signal
            geo_places=len((await ctx.store.get("geo_result")).places)
            if routed.routing.get("needs_geo", False)
            else 0,
        )
        await ctx.store.set("merged", merged)

        audit = await ctx.store.get("audit")
        audit.update(
            sql_query=sql_result.meta.get("sql_query"),
            fact_ids=[e.get("fact_id") for e in sql_result.rows if e.get("fact_id")],
            chunk_ids=[c.get("id") for c in rag_result.chunks if c.get("id")],
            rerank_scores=[c.get("score") for c in chunks],
        )
        await ctx.store.set("audit", audit)

        return MergedEv()

    @step()
    async def generate(self, ctx: Context, ev: MergedEv) -> GeneratedEv:
        merged: Merged = await ctx.store.get("merged")
        routed: RoutedResult = await ctx.store.get("routed")
        # No training-specific grounding gate any more (FR-25 revision): a
        # training turn grounds on the shared project corpus and degrades to
        # the same ungrounded handling as customer mode.
        parts: list[str] = []
        async for token in stream_answer(
            merged, await ctx.store.get("history"), routed.high_stakes
        ):
            token = sanitize_output(token)
            # Buffer every chunk in memory; no SSE_EVENT_TOKEN may leave this
            # step until output_guard has verified the joined answer (LGN-P0-002:
            # verify-before-customer-delivery).
            parts.append(token)
        answer = normalize_answer_display("".join(parts))
        await ctx.store.set("answer", answer)
        audit = await ctx.store.get("audit")
        audit.update(
            model=merged.meta.get("model"),
            answer_tier=merged.meta.get("answer_tier"),  # story 4.6 §7.4: audit tier
            conv_state=merged.meta.get("conv_state"),
            prompt_hash=merged.meta.get("prompt_hash"),
            # NO answer_hash here: P0-05 hashes the FINAL visible string once,
            # at the sealed release point in output_guard. A draft hash would
            # give the same audit column two different meanings.
        )
        await ctx.store.set("audit", audit)
        return GeneratedEv()

    @staticmethod
    def _prompt_evidence_integrity(
        merged: Merged, retrieval_project: str | None
    ) -> tuple[list[dict], list[dict], bool, bool, list[dict]]:
        """``(rag_rows, fact_rows, rag_valid, facts_valid, financial_rows)``.

        ``rag_valid``/``facts_valid`` are STRUCTURAL: prompt rows that
        ``build_messages`` really produced, matching the selected ids, the
        immutable project scope, the fact interval and the UI projection.
        ``financial_rows`` are the structurally valid rows that additionally
        carry a fact id proven against the SQL leg plus current fact, source
        document and campaign lineage for the resolved project.
        """
        rag_rows = merged.meta.get("_prompt_rag_rows")
        fact_rows = merged.meta.get("_prompt_fact_rows")
        if not isinstance(rag_rows, list) or not isinstance(fact_rows, list):
            return [], [], False, False, []
        selected_ids = merged.meta.get("selected_chunk_ids")
        selected_fact_ids = merged.meta.get("selected_fact_ids")
        selected_project_keys = merged.meta.get("selected_project_keys")
        if not isinstance(selected_ids, (list, tuple)) or not isinstance(selected_fact_ids, (list, tuple)):
            return rag_rows, fact_rows, False, False, []
        row_ids = [row.get("id") for row in rag_rows]
        as_of = str(merged.meta.get("as_of") or date.today())
        rag_valid = (
            all(isinstance(row_id, str) and row_id.strip() for row_id in row_ids)
            and len(row_ids) == len(set(row_ids))
            and row_ids == list(selected_ids)
            and all(
                isinstance(row.get("content"), str) and row["content"].strip()
                and isinstance(row.get("doc_id"), str) and row["doc_id"].strip()
                and isinstance(row.get("title"), str) and row["title"].strip()
                and row.get("project_key") == retrieval_project
                and row.get("status") == "published"
                and row.get("effective_from") and str(row["effective_from"]) <= as_of
                and (row.get("effective_to") is None or str(row["effective_to"]) > as_of)
                for row in rag_rows
            )
        )
        expected_project_keys = [retrieval_project] if row_ids else []
        rag_valid = rag_valid and list(selected_project_keys or ()) == expected_project_keys

        row_fact_ids = [row.get("fact_id") for row in fact_rows if row.get("fact_id")]
        row_fe_ids = [row.get("fe_id") for row in fact_rows]
        selected_fe_ids = [row.get("fe_id") for row in merged.facts]
        def _same_project_subject(subject: object) -> bool:
            if not isinstance(subject, str) or not isinstance(retrieval_project, str):
                return False
            return any(
                subject == prefix or subject.startswith(prefix + "/")
                for prefix in (f"unit:{retrieval_project}", f"project:{retrieval_project}")
            )

        # Plan P0-03C precedence: prompt integrity (B) is evaluated BEFORE the
        # price-lookup path. An EXPIRED SELECTED fact row is broken provenance,
        # so it blocks with the grounding copy — it never reaches the probe,
        # whose absence proof would silently paper over the bad selection.
        facts_valid = (
            len(row_fe_ids) == len(set(row_fe_ids))
            and all(isinstance(row_id, str) and row_id.strip() for row_id in row_fe_ids)
            and row_fe_ids == selected_fe_ids
            and row_fact_ids == list(selected_fact_ids)
            and all(
                isinstance(row.get("fields"), dict)
                and _same_project_subject(row.get("subject"))
                and row.get("effective_from") and str(row["effective_from"]) <= as_of
                and (row.get("effective_to") is None or str(row["effective_to"]) > as_of)
                for row in fact_rows
            )
        )
        source_doc_ids = {row.get("doc_id") for row in rag_rows}
        publication_sources_match = (
            len(merged.sources) == len(source_doc_ids)
            and {source.get("doc_id") for source in merged.sources} == source_doc_ids
        )
        # Financial ELIGIBILITY is a separate axis from structural integrity:
        # a properly selected range/estimate row is structurally sound, and
        # only earns authority for a claim that matches its own unit, policy,
        # quality/trust and current three-lineage proof.
        financial_rows = [
            row
            for row in fact_rows
            if facts_valid
            and _sql_confirms_fact(row, merged.meta.get("_fact_evidence") or [])
            and _has_current_financial_lineage(row, retrieval_project, date.today())
        ]
        return (
            rag_rows,
            fact_rows,
            rag_valid and publication_sources_match,
            facts_valid,
            financial_rows,
        )

    @staticmethod
    def _number_free_descriptive(text: str) -> bool:
        return not re.search(r"\d|\[fe-\d{3}\]", text or "")

    @staticmethod
    def _claim_level_numeric_failure(
        verdicts: dict, orphan_spans: list[tuple[int, int]], bound_spans: set[tuple[int, int]]
    ) -> bool:
        """BLOCK only while a verifier-orphan span stays unauthorized.

        A correctly bound range/estimate has no value in ``fields``, so the
        aggregate verifier reports its own bounds as orphans. That false
        negative is WARN-only when EVERY orphan span is exactly accounted for
        by an authenticated claim; any other orphan figure stays BLOCK. The
        aggregate verdict and ``orphan_numbers`` remain verbatim in the audit.
        """
        if verdicts.get("numeric_grounding") != "fail":
            return False
        return any(span not in bound_spans for span in orphan_spans)

    @staticmethod
    async def _confirmed_price_probe(
        project_key: Any, unit_codes: list[str], as_of: date | None
    ) -> ConfirmedPriceProbe:
        """Scoped, read-only absence proof for the resolved project.

        The result is absence evidence only: it never substitutes for a selected
        prompt fact when an amount is published. A failed probe reports
        ``completed=False`` so the caller answers "cannot confirm" instead of
        asserting that a price does not exist.
        """
        if not isinstance(project_key, str) or not project_key.strip():
            return ConfirmedPriceProbe(False, "no_project", frozenset(), False)
        try:
            probe = probe_confirmed_price(project_key, unit_codes, as_of=as_of)
            if inspect.isawaitable(probe):
                probe = await asyncio.wait_for(probe, timeout=STEP_TIMEOUTS["sql"])
        except Exception as exc:  # noqa: BLE001 — a failed probe is "unknown"
            return ConfirmedPriceProbe(False, type(exc).__name__, frozenset(), False)
        return probe


    @step()
    async def output_guard(self, ctx: Context, ev: GeneratedEv) -> StopEvent:
        merged: Merged = await ctx.store.get("merged")
        answer: str = await ctx.store.get("answer")
        routed: RoutedResult = await ctx.store.get("routed")
        query_text = str(merged.meta.get("query") or "")
        retrieval_project = merged.meta.get("retrieval_project_key") or merged.meta.get(
            "project_key"
        )
        requested_units = extract_unit_codes(query_text)
        exact_price_request = bool(requested_units) and is_unit_price_query(query_text)
        rag_rows, fact_rows, rag_valid, facts_valid, financial_rows = (
            self._prompt_evidence_integrity(merged, retrieval_project)
        )
        merged.meta["selected_prompt_chunk_ids"] = [row.get("id") for row in rag_rows]
        merged.meta["selected_prompt_fact_ids"] = [row.get("fact_id") for row in fact_rows]
        # SAFETY INTERLOCK, not an optimisation: a chunk set that failed scope,
        # provenance, status or date validation authorizes NOTHING. Publishing a
        # figure because a foreign or expired chunk happened to state it is
        # exactly the cross-project leak the integrity gate exists to stop, so
        # the empty list is the fail-closed answer and must not be relaxed.
        merged.meta["_chunk_numeric_evidence"] = (
            _chunk_numeric_evidence(rag_rows) if rag_valid else []
        )
        # Same interlock, same `rag_valid`: a figure being PRESENT in a chunk is
        # not proof it BELONGS to the subject the answer names, and this list is
        # what lets the verifier veto a misattribution. Sharing the one validity
        # decision is the point -- deriving it from a second condition would let
        # the two authorities disagree about which chunks may speak at all.
        merged.meta["_chunk_binding_evidence"] = (
            _chunk_binding_evidence(rag_rows) if rag_valid else []
        )
        # Same interlock, same `rag_valid`. The verifier already treats a
        # percentage as a dimension a validated chunk may state (`_CURRENCY_UNITS`
        # excludes "%"), while the financial claim binder classified every
        # percentage as a claim only a SQL fact row could license. That split is
        # what withheld answers whose percentage was quoted faithfully from a
        # retrieved chunk: one gate authorized the figure and the other canned the
        # turn. Feeding the binder the chunk's own percentages closes the gap
        # without loosening money, which stays fact-only.
        merged.meta["_chunk_pct_evidence"] = (
            _chunk_pct_evidence(rag_rows) if rag_valid else []
        )
        # An exact-unit price question verifies against the lineage-proven rows
        # for the requested units only; every other turn keeps the whole UI
        # projection so registry-backed amenity areas stay groundable.
        observed_facts = (
            [
                row
                for row in financial_rows
                if _unit_subject_code(row.get("subject"), retrieval_project) in requested_units
            ]
            if exact_price_request
            else merged.facts
        )
        # One ungrounded closing sentence must not cost the whole answer. The
        # prompt asks for a short benefit summary, and the model writes it by
        # combining bounds from several methods ("từ 1,72 tỷ đến 2,64 tỷ"), which
        # no single row authorizes. Dropping exactly those sentences keeps the
        # per-method bands that bound, and the helper re-binds the remainder
        # before offering it -- so a repair that would launder a figure is
        # refused and the canned copy below still runs.
        #
        # This runs BEFORE the verifier, not after: the guard's verdicts and
        # orphan list are what the publish gate reads, and a verdict taken on a
        # string that is then replaced would refuse a turn whose shipped answer
        # grounds clean. The verifier must judge the string that ships.
        repaired = (
            _drop_ungrounded_sentences(
                answer, financial_rows, query_text, retrieval_project, merged.meta
            )
            if not merged.meta.get("answer_complete")
            else None
        )
        if repaired is not None:
            logger.warning(
                "draft repaired: dropped %d chars of ungrounded prose",
                len(answer) - len(repaired),
            )
            await self._flag(ctx, "draft_repaired_ungrounded_prose")
            answer = repaired
            await ctx.store.set("answer", answer)
        verification_failed = False
        try:
            guard_res = await asyncio.wait_for(
                guard_output(answer, observed_facts, merged.sources, routed.routing, meta=merged.meta),
                timeout=STEP_TIMEOUTS["output_guard"],
            )
        except asyncio.TimeoutError:
            verification_failed = True
            guard_res = OutputGuardResult(
                confidence="LOW",
                requires_review=True,
                verdicts={"timeout": True, "verification_unavailable": True},
            )
            await self._flag(ctx, "output_guard_timeout")
        except Exception as exc:  # noqa: BLE001 — verification failure is never silent
            # Only the exception CLASS is recorded: a verifier message can carry
            # query/SQL fragments and must not reach the audit trail or flags.
            verification_failed = True
            error_type = type(exc).__name__
            guard_res = OutputGuardResult(
                confidence="LOW",
                requires_review=True,
                verdicts={"verification_unavailable": True, "error_type": error_type},
            )
            await self._flag(ctx, f"output_guard_error:{error_type}")

        # A LOW result can describe either a completed, reviewable answer or a
        # grounding failure. Never publish unsupported money or orphan citations;
        # other completed LOW verdicts remain useful when selected evidence is
        # present and demonstrably scoped to the resolved project.
        unresolved_scope = not isinstance(retrieval_project, str) or not retrieval_project.strip()
        integrity_failed = (
            (bool(merged.meta.get("selected_chunk_ids") or merged.sources) and not rag_valid)
            or (bool(merged.meta.get("selected_fe_ids") or merged.facts) and not facts_valid)
            or (bool(rag_rows) and not rag_valid)
            or (bool(fact_rows) and not facts_valid)
        )
        has_rag_area_evidence = bool(merged.meta.get("_rag_area_evidence")) and rag_valid and bool(rag_rows)
        selected_evidence = [*merged.sources, *merged.facts]
        selected_project_keys = [
            *merged.meta.get("selected_project_keys", []),
            *[
                item.get("project_key")
                for item in selected_evidence
                if item.get("project_key")
            ],
            *[
                item[5]
                for item in merged.meta.get("_rag_area_evidence", [])
                if isinstance(item, (tuple, list)) and len(item) == 6
            ],
        ]
        cross_project_evidence = bool(retrieval_project) and any(
            key != retrieval_project for key in selected_project_keys
        )
        current_selected_fact = bool(fact_rows) and facts_valid
        current_selected_rag = bool(rag_rows) and rag_valid
        usable_evidence = (
            not unresolved_scope
            and not cross_project_evidence
            and not integrity_failed
            and (current_selected_fact or current_selected_rag or has_rag_area_evidence)
        )
        factual_answer_without_evidence = (
            not self._number_free_descriptive(answer)
            and not (current_selected_fact or current_selected_rag or has_rag_area_evidence)
        )

        # Per-claim binding. A figure is authorized only by a selected,
        # SQL-confirmed, currently lineaged row whose OWN clause names it; an
        # aggregate HIGH verdict never grants authority for an unbound figure.
        bound_spans, claim_span_total, published_estimate = _bind_financial_claims(
            financial_rows, answer, query_text, retrieval_project, merged.meta
        )
        unsupported_figure = claim_span_total > len(bound_spans)
        verdicts = guard_res.verdicts or {}
        citation_status = (verdicts.get("citation_grounding") or {}).get("status")
        # A ``pending`` citation verdict is only publishable when the answer
        # carries no [fe-xxx] at all; a cited id that could not be checked
        # remains a block, and an uncited pending answer must be reviewed.
        pending_citation_without_id = citation_status == "pending" and not re.search(
            r"\[fe-\d{3}\]", answer
        )
        # The grounding axes are named individually rather than left fused in one
        # expression because the audit flag below must report WHICH axis refused
        # the turn; an anonymous boolean can only ever say "it was blocked".
        orphan_spans = _verifier_orphan_spans(answer, observed_facts, merged.meta)
        numeric_ungrounded = self._claim_level_numeric_failure(
            verdicts, orphan_spans, bound_spans
        )
        citation_ungrounded = citation_status == "fail" or (
            citation_status == "pending" and not pending_citation_without_id
        )
        grounding_failed = (
            numeric_ungrounded
            or citation_ungrounded
            or unsupported_figure
            or factual_answer_without_evidence
            or integrity_failed
            or unresolved_scope
        )

        price_question = is_unit_price_query(query_text)
        sql_result: SqlLegResult | None = await ctx.store.get("sql_result")
        scoped_lookup = (
            routed.routing.get("needs_sql", False)
            and not unresolved_scope
            and sql_result is not None
            and not sql_result.degraded
            and not sql_result.meta.get("error")
            and sql_result.meta.get("mode") != "none"
        )
        # Verifier outage is a WARN, not a BLOCK -- but only while the answer's
        # figures can still be checked without the verifier. `safe_number_free`
        # used to stand in for that check and demanded a figure-FREE draft, which
        # is the opposite of a useful answer: every grounded price, area or unit
        # count disqualified itself, so a correct answer was deleted and replaced
        # by canned copy precisely because it was informative. The structural
        # check below replaces it and is strictly better targeted -- each figure
        # must still be authorized, money by a fact alone.
        #
        # `usable_evidence` is already the conjunction of scope soundness and
        # evidence integrity (`not unresolved_scope and not cross_project_evidence
        # and not integrity_failed`), so the outage path needs no second guard
        # against those: it cannot fire on a turn whose scope never resolved.
        # High-stakes turns keep blocking outright: an unavailable verifier must
        # not become the route by which a legal or financial answer ships
        # unchecked.
        outage_figure_unbacked = verification_failed and not _outage_figures_grounded(
            answer, observed_facts, merged.meta
        )
        verifier_warning = (
            verification_failed
            and usable_evidence
            and not routed.high_stakes
            and not outage_figure_unbacked
        )
        # A price-availability question must be answered with a price or with a
        # proven absence. A draft that quietly ignores the question is not a
        # grounded answer to it, however well formed it verifies.
        # Plan P0-03C: a generic price question with no confirmed eligible fact
        # after a successful lookup gets the generic limitation copy — never the
        # draft's off-topic prose.
        price_question_unanswered = price_question and not _financial_claims(answer)
        live_related_turn = (
            bool(merged.meta.get("answer_complete"))
            and not unresolved_scope
            and not cross_project_evidence
            and not (
                bool(merged.meta.get("selected_chunk_ids") or merged.sources) and not rag_valid
            )
            and (current_selected_rag or current_selected_fact or has_rag_area_evidence)
            and bool(answer and answer.strip())
            and not (verification_failed and routed.high_stakes)
        )
        publish_original = live_related_turn or (
            usable_evidence
            and not grounding_failed
            and not price_question_unanswered
            and (not verification_failed or verifier_warning)
        )
        # The invariant this flag exists to prove is "selected chunks + a
        # resolved scope + intact evidence integrity => an answer ships".
        # Asserting that in prose proves nothing, so the decision records its own
        # reason: the value stays None exactly when the DRAFT was published, and
        # otherwise names the one evidence-side fact that refused it. That makes
        # a legitimate refusal distinguishable from the regression this flag was
        # added to catch, and no canned copy can ship without accounting for
        # itself. Values come from ``FALLBACK_REASONS``.
        fallback_reason: str | None = None
        if published_estimate or pending_citation_without_id:
            # A bound estimate and an uncheckable-but-uncited answer both ship as
            # reviewed content: the figures are authenticated, the verdict is not.
            guard_res = OutputGuardResult(
                confidence=guard_res.confidence,
                requires_review=True,
                verdicts=guard_res.verdicts,
            )

        publish_limitation = False
        limitation_reason = ""
        if not publish_original:
            # The safe copy follows the BLOCK REASON, never the verifier's
            # confidence: a HIGH/MEDIUM verdict says the answer is well formed,
            # not that its evidence is publishable. Only a grounding failure
            # (unsupported numbers/citations, or prompt rows that lost their
            # selected provenance) is a grounding problem. Verifier unavailable,
            # unresolved or cross-project scope and missing evidence are all
            # verification problems, and none of them may keep a byte of the
            # draft — only a proven limitation may replace the answer.
            scope_sound = not (unresolved_scope or cross_project_evidence or integrity_failed)
            if price_question and scope_sound:
                # Absence may only be asserted from a COMPLETED scoped lookup;
                # a failed probe, or an unavailable verifier on this turn, is
                # uncertainty — never absence.
                probe = (
                    await self._confirmed_price_probe(
                        retrieval_project, requested_units, await ctx.store.get("as_of_date")
                    )
                    if scoped_lookup and not verification_failed
                    else None
                )
                missing_units = [
                    code
                    for code in dict.fromkeys(requested_units)
                    if probe is not None and code not in probe.confirmed_units
                ]
                # A selected, lineage-proven range is evidence the project HAS a price, so
                # absence must never be asserted while it exists. Only a PRICE range
                # (not a rate or deposit range) counts: a range row with a bound price
                # field, scoped to the requested units when a unit was asked for.
                range_rows = [
                    row
                    for row in financial_rows
                    if _fact_range_bounds(row) is not None
                    and any(name in _PRICE_FIELDS for name in (row.get("fields") or {}))
                    and (
                        not requested_units
                        or _unit_subject_code(row.get("subject"), retrieval_project)
                        in requested_units
                    )
                ]
                if probe is None or not probe.completed:
                    answer, limitation_reason = PRICE_LOOKUP_UNCERTAIN, "price_lookup_unavailable"
                elif probe.any_confirmed and unsupported_figure:
                    # A confirmed price exists but did not validate this figure:
                    # claiming absence would be the less truthful copy.
                    answer, limitation_reason = PRICE_LOOKUP_UNCERTAIN, "unvalidated_confirmed_price"
                elif range_rows and not probe.any_confirmed:
                    # Range evidence is real, but this turn cannot quote it as a
                    # publishable figure, so the copy says "cannot confirm", not absence.
                    answer, limitation_reason = PRICE_LOOKUP_UNCERTAIN, "range_evidence_unbound"
                elif missing_units:
                    answer = " ".join(
                        NO_CONFIRMED_UNIT_PRICE.format(unit_code=code) for code in missing_units
                    )
                    limitation_reason = "no_confirmed_unit_price"
                elif not probe.any_confirmed:
                    answer, limitation_reason = NO_CONFIRMED_PROJECT_PRICE, "no_confirmed_project_price"
                else:
                    answer, limitation_reason = PRICE_LOOKUP_UNCERTAIN, "price_lookup_unavailable"
                publish_limitation = True
                fallback_reason = "price_unanswered"
            else:
                grounding_block = (
                    not verification_failed
                    and not unresolved_scope
                    and not cross_project_evidence
                    and grounding_failed
                )
                answer = LOW_GROUNDING_MESSAGE if grounding_block else VERIFICATION_FALLBACK_MESSAGE
                fallback_reason = _canned_fallback_reason(
                    grounding_block=grounding_block,
                    verification_failed=verification_failed,
                    outage_figure_unbacked=outage_figure_unbacked,
                    unresolved_scope=unresolved_scope,
                    cross_project_evidence=cross_project_evidence,
                    integrity_failed=integrity_failed,
                    numeric_ungrounded=numeric_ungrounded,
                    citation_ungrounded=citation_ungrounded,
                    unsupported_figure=unsupported_figure,
                )
            # Observability only: a rejected draft must be diagnosable from the
            # log without shipping customer data at WARNING. The draft body is
            # DEBUG-bounded (first 500 chars) because WARNING records reach
            # operator dashboards where customer query text must not appear.
            logger.warning(
                "publish blocked: reason=%s answer_chars=%d unsupported=%s",
                fallback_reason,
                len(answer),
                unsupported_figure,
            )
            logger.debug("publish blocked draft: %.500s", answer)
            # Every replaced answer is a canned safe copy, so it never inherits
            # the verifier's tier: that tier describes the REJECTED draft. A HIGH
            # verdict on an unverifiable price claim must still ship LOW/review.
            guard_res = OutputGuardResult(
                confidence="LOW",
                requires_review=True,
                verdicts={
                    **(guard_res.verdicts or {}),
                    **({"price_limitation": limitation_reason} if publish_limitation else {}),
                },
            )
            if publish_limitation:
                await self._flag(ctx, f"price_limitation:{limitation_reason}")
        publish_rag = publish_original and current_selected_rag
        publish_facts = publish_original and current_selected_fact

        # --- P0-05 D2/D5: the string that ships IS the string that is audited.
        # The bound applies to the ORIGINAL allowed candidate only. A canned
        # safety / limitation / fallback copy is emitted COMPLETE (D2 step 7):
        # truncating it would silently drop a unit the user actually asked
        # about, which is worse than exceeding a tiny budget. Such a copy
        # carries no figure or citation, so it needs no bound of its own.
        from api.application.services.global_assistant_service import (  # noqa: PLC0415
            _bound_visible,
        )

        visible_limit = await ctx.store.get("visible_limit")
        if publish_original:
            canonical = sanitize_answer_text(answer, terminated=True)
            bounded = _bound_visible(canonical, visible_limit)
            final_answer = sanitize_answer_text(bounded, terminated=True)
            if _bound_visible(final_answer, visible_limit) != final_answer:
                # The truncation suffix is itself a blocked bracket group, so
                # it only disappears on the next identical pass.
                final_answer = sanitize_answer_text(
                    _bound_visible(final_answer, visible_limit), terminated=True
                )
        else:
            canonical = answer
            bounded = answer
            final_answer = answer
        # D2: shortening a money/estimate/high-stakes answer can silently drop
        # the qualification that made it publishable (a trailing "ước tính", a
        # VAT clause, one range endpoint). This interface exposes no span
        # contract for that, so a CHANGED qualified answer is replaced by the
        # complete number-free uncertainty copy rather than truncated.
        qualified = bool(published_estimate or routed.high_stakes or _financial_claims(answer))
        shortened = bounded != canonical
        # Two independent causes reach the substitution below, and they are NOT
        # the same failure. (a) A caller-imposed visible bound clipped the
        # qualification that made a qualified answer publishable. (b) The final
        # recheck found a figure the finished string cannot ground -- which a
        # sanitizer pass can create by removing an internal marker that was
        # separating a number from its unit. Reporting both under the bound's
        # token named an event that never happened and hid a grounding failure
        # behind it, so each cause is tracked and reported on its own.
        visible_bound_dropped = bool(shortened and qualified)
        final_blocked = visible_bound_dropped
        changed = final_answer != answer
        if changed and not final_blocked:
            # D2.6: re-check the real final string. The evidence selected on
            # this turn is the input; no new absence proof is invented here. An
            # unchanged answer reuses its completed check.
            final_bound, final_total, _ = _bind_financial_claims(
                financial_rows, final_answer, query_text, retrieval_project, merged.meta
            )
            final_orphans = _verifier_orphan_spans(final_answer, observed_facts, merged.meta)
            # No citation gate here on purpose: `final_answer` has already been
            # canonicalized, and the sanitizer strips EVERY [fe-NNN] marker, so
            # the set would always be empty. The pre-sanitization decision is
            # the citation authority; bounding can only remove markers.
            final_blocked = (
                final_total > len(final_bound)
                or any(span not in final_bound for span in final_orphans)
                or (
                    not self._number_free_descriptive(final_answer)
                    and not (current_selected_fact or current_selected_rag or has_rag_area_evidence)
                )
            )
        if final_blocked:
            # D2.7: the number-free copy carries no figure and no citation, so
            # it re-checks clean by construction — and it takes none of the
            # rejected draft's evidence rights with it.
            final_answer = LOW_GROUNDING_MESSAGE
            publish_rag = False
            publish_facts = False
            # Whichever cause fired names itself. `visible_bound_dropped` was
            # decided before the recheck ran and the recheck only runs when it
            # was False, so the two are mutually exclusive by construction and
            # this choice cannot mislabel a grounding failure as a bound event.
            fallback_reason = (
                "visible_bound_dropped_qualification"
                if visible_bound_dropped
                else "final_recheck_ungrounded"
            )
            guard_res = OutputGuardResult(
                confidence="LOW", requires_review=True, verdicts=guard_res.verdicts,
            )
        answer = final_answer
        if fallback_reason is not None:
            # Two records, two readers: `degraded` is the channel operators
            # already watch, and the audit field written below is the
            # machine-checkable one the publication test reads. Both are emitted
            # at the decision branch, so neither can drift from the answer that
            # actually shipped.
            await self._flag(ctx, f"fallback_reason:{fallback_reason}")
        # A safety copy is never truncated, so one can still outgrow the budget
        # (the per-unit price copy grows with the number of units asked about,
        # and MIN_VISIBLE_CHARS only bounds the STATIC copies). Emitting it
        # anyway would ship an over-budget body event, and the GA watchdog only
        # sees the answer after this call returns — too late. Refuse the turn
        # here instead: same L1 mapping, and no body, token, or evidence event
        # is released.
        if visible_limit is not None and len(answer) > visible_limit:
            audit = await ctx.store.get("audit")
            # Merge, never replace: the completed output-guard decision stays on
            # the record and the refusal reason is added to it.
            audit["guard_verdicts"] = {
                **(audit.get("guard_verdicts") or {}),
                **(guard_res.verdicts or {}),
                "L1": "reject", "reason": "safe_copy_exceeds_visible_limit",
            }
            audit["fallback_reason"] = fallback_reason
            await ctx.store.set("audit", audit)
            await self._write_audit(ctx)
            raise QueryRejected("bản an toàn dài hơn giới hạn hiển thị")
        audit = await ctx.store.get("audit")
        audit.update(
            confidence=guard_res.confidence,
            # None is the proof: the drafted answer was published. Any other
            # value names the evidence-side fact that replaced it with canned
            # copy, so this single field is what makes the owner invariant
            # checkable after the fact instead of merely asserted in a comment.
            fallback_reason=fallback_reason,
            guard_verdicts={
                **(guard_res.verdicts or {}),
                # D5: final-visible diagnostics live under their own key so a
                # rejected draft's numeric_grounding="fail" is never rewritten
                # into a misleading pass.
                "final_visible": {
                    "bounded": shortened,
                    "canonicalized": changed,
                    "shortened_qualified": final_blocked and shortened and qualified,
                    "recheck_blocked": final_blocked,
                },
                # D6: the rows build_messages REALLY serialized, inside the
                # existing verdict JSON. audit.chunk_ids stays retrieval-only.
                "selected_prompt_chunk_ids": list(
                    merged.meta.get("selected_prompt_chunk_ids") or ()
                ),
                "selected_prompt_fact_ids": list(
                    merged.meta.get("selected_prompt_fact_ids") or ()
                ),
            },
            answer_hash=sha256_hex(answer),
        )
        latency_ms = int((time.perf_counter() - await ctx.store.get("t0")) * 1000)
        audit.update(latency_ms=latency_ms, degraded=await ctx.store.get("degraded"))
        await ctx.store.set("audit", audit)
        await self._write_audit(ctx)
        if publish_rag:
            if routed.routing.get("needs_geo", False):
                await self._emit(SSE_EVENT_PLACES, {"places": _places_payload(await ctx.store.get("geo_result"))})
            await self._emit(SSE_EVENT_SOURCES, {"sources": merged.sources})
        if publish_facts:
            await self._emit(SSE_EVENT_FACTS, {"facts": await ctx.store.get("presentation_facts")})
        if publish_rag:
            await self._emit(SSE_EVENT_IMAGES, {"images": await ctx.store.get("presentation_images")})
        await self._emit(SSE_EVENT_TOKEN, {"text": answer})
        return StopEvent(result={
            "answer": answer,
            "sources": merged.sources if publish_rag else [],
            "facts": _presentation_facts(merged.facts, retrieval_project, requested_units) if publish_facts else [],
            "images": (await ctx.store.get("presentation_images") or []) if publish_rag else [],
            "places": _places_payload(await ctx.store.get("geo_result")) if publish_rag and routed.routing.get("needs_geo", False) else [],
            "confidence": guard_res.confidence,
            "requires_review": guard_res.requires_review,
            "routing": routed.routing,
            "trace_id": await ctx.store.get("trace_id"),
            "latency_ms": latency_ms,
        })




class RagQueryPipeline:
    """Back-compat facade: `await run(**kwargs) -> dict` over the workflow.

    Keeps eval/run_eval.py and main.py's existing call contract; the workflow
    emits SSE events through the optional on_event callback during steps.
    """

    def __init__(self, on_event: EventCallback | None = None):
        self._on_event = on_event

    async def run(
        self,
        query: str,
        session_id: str | None = None,
        as_of: str | None = None,
        history: list[dict] | None = None,
        *,
        project_context: ProjectContext,
        device_id: str | None = None,
        on_event: EventCallback | None = None,
        visible_limit: int | None = None,
    ) -> dict:
        from api.application.services.project_config import (  # noqa: PLC0415
            bound_request_project,
            load_project_registry_record,
        )

        record = await load_project_registry_record(project_context.project_key)
        with bound_request_project(record):
            wf = RagQueryWorkflow(on_event=on_event if on_event is not None else self._on_event)
            return await wf.run(
                query=query,
                session_id=session_id,
                as_of=as_of,
                history=history or [],
                project_context=project_context,
                device_id=device_id,
                visible_limit=visible_limit,
            )


def _places_payload(result: GeoResult | None) -> list[dict[str, Any]]:
    """GeoResult -> JSON-safe place list for SSE events + maps rendering."""
    if result is None:
        return []
    return [
        {
            "name": p.name,
            "kinds": list(p.kinds),
            "lat": p.lat,
            "lng": p.lng,
            "distance_m": p.distance_m,
            "address": p.address,
            "rating": p.rating,
        }
        for p in result.places
    ]
