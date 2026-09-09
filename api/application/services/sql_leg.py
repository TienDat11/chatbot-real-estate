"""SQL leg — deterministic spec-builder (R1) plus the R2 NL2SQL route (plan §4.4, §3.8).

R1 validates sql_spec against a closed set, builds a parameterized query, and runs
it in a RLS transaction (SET LOCAL ROLE ro_query). Numbers are never computed by
the LLM — only cited from facts/view (AD-15). match_semantics applies range/approx
semantics (plan §4.4 A8); SQL yields a superset, Python filters exactly. R2
delegates to api.nl2sql_guard when structured_path == 'nl2sql'.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import asyncpg

from api import get_cfg
from api.application.services.fact_display import policy_display, subject_display
from api.domain.entities.price_calc import (
    affordability_rows,
    affordability_summary,
    analyze_affordability,
    offer_from_row,
)

logger = logging.getLogger("api.sql_leg")

# Closed-set allowlist (plan §4.4 — validated before building).
ALLOWED_SOURCES = ("facts", "v_unit_offers")

ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "v_unit_offers": (
        "subject_id",
        "policy_key",
        "price_vnd",
        "deposit_pct",
        "term_months",
        "interest_rate_pct",
        "required_down_payment_vnd",
        "loan_amount_vnd",
        "monthly_principal_vnd",
        "monthly_interest_estimate_vnd",
    ),
    "facts": (
        "price_vnd",
        "area_m2",
        "deposit_pct",
        "term_months",
        "interest_rate_pct",
        "subject_key",
        "subject_type",
        "value_num",
        "value_text",
        "unit",
        "quality",
        "policy_key",
        "campaign_key",
    ),
}

# Semantic fact_key fields on 'facts' — filtered by value_num/range.
SEMANTIC_FACT_FIELDS = {"price_vnd", "area_m2", "deposit_pct", "term_months", "interest_rate_pct"}

ALLOWED_OPS = ("=", "!=", "<", "<=", ">", ">=", "between", "in")
ALLOWED_DIR = {"asc": "ASC", "desc": "DESC"}
# Structured catalogue/payment questions must not inherit the router's usual
# top-k cap. 200 is bounded, parameter-free, and covers both seeded projects.
MIN_LIMIT, MAX_LIMIT, DEFAULT_LIMIT = 1, 200, 10
_EXHAUSTIVE_QUERY_TERMS = (
    "tung loai",
    "moi loai",
    "cac loai",
    "tat ca",
    "toan bo",
    "danh muc",
    "phuong thuc thanh toan",
    "phuong an thanh toan",
    "payment method",
    "unit catalog",
)

# Roles allowed for the transaction-local role GUC — checked fail-closed before
# anything reaches the connection (Mimosa finding: latent injection surface).
# All current callers rely on the default, so the set stays minimal by design.
ALLOWED_RLS_ROLES = frozenset({"ro_query"})

OFFER_COLUMNS = (
    "subject_id",
    "policy_key",
    "price_vnd",
    "deposit_pct",
    "term_months",
    "interest_rate_pct",
    "required_down_payment_vnd",
    "loan_amount_vnd",
    "monthly_principal_vnd",
    "monthly_interest_estimate_vnd",
)


class SpecError(ValueError):
    """Spec violates the closed set — caller degrades to RAG-only (§4.4)."""


class SqlLegError(Exception):
    """SQL leg execution failure (timeout/DB) — caller degrades."""


@dataclass
class SqlLegResult:
    rows: list[dict] = field(default_factory=list)  # FACT_EVIDENCE blocks (fe-...)
    meta: dict = field(default_factory=dict)  # {mode, source, sql, sql_query, row_count, error}
    degraded: bool = False


# RO pool — owner connects; SET LOCAL ROLE ro_query runs inside each transaction.
_ro_pool: asyncpg.Pool | None = None


def build_dsn() -> str:
    """Build the DSN from Settings (no hardcoding)."""
    host = get_cfg("postgres_host", "localhost")
    port = get_cfg("postgres_port", 5432)
    user = get_cfg("postgres_user", "ragre")
    password = urllib.parse.quote(str(get_cfg("postgres_password", "")), safe="")
    db = get_cfg("postgres_database", "ragre")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _pooler_safe_kwargs() -> dict[str, int]:
    """Extra asyncpg connect kwargs that disable server-side prepared statements.

    Supavisor (:6543 / pooler.supabase.com) does not support prepared statements
    and will raise DuplicatePreparedStatementError if caching is enabled.
    Passing these to direct-postgres connections (:5432) is harmless — the
    client simply skips the prepare round-trips and inlines the SQL.
    """
    return {
        "statement_cache_size": 0,
        "max_cached_statement_lifetime": 0,
        "max_cacheable_statement_size": 0,
    }


async def get_ro_pool() -> asyncpg.Pool:
    """Lazy singleton RO pool; role switching happens inside each transaction."""
    global _ro_pool
    if _ro_pool is None or _ro_pool.is_closing():
        _ro_pool = await asyncpg.create_pool(
            build_dsn(),
            min_size=1,
            max_size=int(get_cfg("postgres_max_connections", 5) or 5),
            **_pooler_safe_kwargs(),
        )
    return _ro_pool


async def close_ro_pool() -> None:
    global _ro_pool
    if _ro_pool is not None and not _ro_pool.is_closing():
        await _ro_pool.close()
    _ro_pool = None


@asynccontextmanager
async def with_rls_identity(
    timeout_s: float = 2.0,
    role: str = "ro_query",
    pool: asyncpg.Pool | None = None,
) -> AsyncIterator[asyncpg.Connection]:
    """One-place transaction helper (plan §3.5): BEGIN, SET LOCAL statement_timeout,

    SET LOCAL ROLE, yield, COMMIT. Shares the SQL leg, hydrate, and post-filter paths.
    """
    # Fail-closed before any pool/connection work: nothing outside the
    # allowlist may reach the role GUC.
    if role not in ALLOWED_RLS_ROLES:
        raise SpecError(f"role không hợp lệ: {role!r} (cho phép {sorted(ALLOWED_RLS_ROLES)})")
    pool = pool or await get_ro_pool()
    conn: asyncpg.Connection = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        # SET LOCAL has no bind-parameter form, so both settings go through
        # set_config with is_local=true (transaction-scoped) and fully bound
        # values; the role is additionally gated by the allowlist above.
        await conn.execute(
            "SELECT set_config('statement_timeout', $1, true)",
            f"{int(timeout_s * 1000)}ms",
        )
        await conn.execute("SELECT set_config('role', $1, true)", role)
        yield conn
        await tr.commit()
    except BaseException:
        await tr.rollback()
        raise
    finally:
        await pool.release(conn)


# R1 — validate, build, match semantics.
def _coerce_limit(limit: Any) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise SpecError(f"limit phải là int, got {type(limit).__name__}")
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise SpecError(f"limit ngoài [{MIN_LIMIT},{MAX_LIMIT}]")
    return limit


def validate_spec(spec: dict | None) -> None:
    """Validate the closed set before building SQL; raise SpecError on violations."""
    if not isinstance(spec, dict):
        raise SpecError("spec phải là object")
    source = spec.get("source")
    if source not in ALLOWED_SOURCES:
        raise SpecError(f"source không hợp lệ: {source!r} (cho phép {ALLOWED_SOURCES})")

    allowed = ALLOWED_FIELDS[source]
    filters = spec.get("filters") or []
    if not isinstance(filters, list):
        raise SpecError("filters phải là list")
    for f in filters:
        if not isinstance(f, dict):
            raise SpecError("mỗi filter phải là object")
        field_name = f.get("field")
        op = f.get("op")
        value = f.get("value")
        if field_name not in allowed:
            raise SpecError(f"field '{field_name}' không nằm trong allowlist {source}")
        if op not in ALLOWED_OPS:
            raise SpecError(f"op '{op}' không nằm trong allowlist {ALLOWED_OPS}")
        if op == "between":
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                raise SpecError("between cần value là [lo, hi]")
        elif op == "in":
            if not (isinstance(value, (list, tuple)) and len(value) >= 1):
                raise SpecError("in cần value là list không rỗng")

    order_by = spec.get("order_by") or {}
    if order_by:
        if not isinstance(order_by, dict):
            raise SpecError("order_by phải là object")
        if order_by.get("field") not in allowed:
            raise SpecError(f"order_by.field '{order_by.get('field')}' không hợp lệ")
        if order_by.get("dir") not in ALLOWED_DIR:
            raise SpecError(f"order_by.dir '{order_by.get('dir')}' không hợp lệ")

    _coerce_limit(spec.get("limit", DEFAULT_LIMIT))


def _filter_sql(source: str, f: dict, params: list[Any]) -> str:
    """Return the WHERE clause for one filter, appending params (asyncpg is 1-based)."""
    field_name, op, value = f["field"], f["op"], f["value"]
    is_vnd = source == "v_unit_offers"

    def next_param(v: Any) -> str:
        params.append(v)
        return f"${len(params)}"

    if op == "between":
        lo, hi = value
        if is_vnd:
            return f"{field_name} BETWEEN {next_param(lo)} AND {next_param(hi)}"
        # facts: superset overlap
        return (
            f"(f.value_num BETWEEN {next_param(lo)} AND {next_param(hi)} "
            f"OR (f.range_min <= {params[-1]} AND f.range_max >= {params[-2]}))"
        )
    if op == "in":
        if is_vnd:
            return f"{field_name} = ANY({next_param(list(value))})"
        # facts: categorical only — match_semantics drops non-matching numeric rows
        return f"f.value_text = ANY({next_param([str(v) for v in value])})"

    p = next_param(value)
    if is_vnd:
        return f"{field_name} {op} {p}"

    # source = facts
    if field_name in SEMANTIC_FACT_FIELDS:
        if op in ("=", "!="):
            # match exact rows only; '=' never matches a range, so also constrain value_num
            fk = next_param(field_name)
            return f"f.fact_key = {fk} AND f.value_num {op} {p} AND f.quality = 'exact'"
        if op in ("<", "<="):
            return (
                f"f.fact_key = {next_param(field_name)} AND "
                f"(f.value_num {op} {p} OR f.range_min {op} {p})"
            )
        if op in (">", ">="):
            return (
                f"f.fact_key = {next_param(field_name)} AND "
                f"(f.value_num {op} {p} OR f.range_max {op} {p})"
            )
    if field_name == "subject_key":
        return f"fs.subject_key = {p}"
    if field_name == "subject_type":
        return f"fs.subject_type = {p}"
    if field_name in ("policy_key", "campaign_key", "unit", "quality", "value_text"):
        col = f"f.{field_name}"
        if op in ("=", "!="):
            return f"{col} {op} {p}"
    if field_name == "value_num":
        return f"f.value_num {op} {p}"

    raise SpecError(f"không build được filter field={field_name} op={op} trên {source}")


def build_sql(
    spec: dict, as_of: date | None, project_key: str | None = None
) -> tuple[str, list[Any]]:
    """Build the parameterized SQL; raise SpecError for an invalid pre-validated spec."""
    source = spec["source"]
    params: list[Any] = []
    as_of = as_of or date.today()  # None -> today, avoiding NULL comparisons

    if source == "v_unit_offers":
        cols = ", ".join(OFFER_COLUMNS)
        # Route through the parameterized function so historical as_of binds (the view
        # pins CURRENT_DATE for backward-compatible consumers).
        sql = f"SELECT {cols} FROM v_unit_offers_as_of(${_next(params, as_of)})"
        where: list[str] = []
        if project_key:
            # The offer view is not project-aware (legacy single-project view), so
            # scope it to the project's subjects at the SQL level — a Soleil query
            # must never surface a Camellia offer (story 10.4).
            where.append(
                f"subject_id IN (SELECT id FROM fact_subjects "
                f"WHERE project_key = ${_next(params, project_key)})"
            )
        for f in spec.get("filters") or []:
            where.append(_filter_sql(source, f, params))
        if where:
            sql += " WHERE " + " AND ".join(where)
    else:
        # facts: join fact_subjects; enforce interval validity at as_of and a published
        # doc (defense-in-depth; RLS FORCE still guards if ever forgotten).
        sql = (
            "SELECT f.id AS fact_id, fs.subject_key, fs.subject_type, fs.display_name, "
            "f.fact_key, f.policy_key, f.campaign_key, f.value_num, f.value_text, f.unit, f.quality, "  # noqa: E501
            "f.range_min, f.range_max, f.effective_from, f.effective_to, "
            "f.source_doc_id, f.source_chunk_id, f.trust_level "
            "FROM facts f JOIN fact_subjects fs ON fs.id = f.subject_id"
        )
        where = [
            f"f.effective_from <= ${_next(params, as_of)}",
            f"(f.effective_to IS NULL OR f.effective_to > ${_next(params, as_of)})",
            f"EXISTS (SELECT 1 FROM documents d WHERE d.doc_id = f.source_doc_id "
            f"AND d.status = 'published' AND d.effective_from <= ${_next(params, as_of)} "
            f"AND (d.effective_to IS NULL OR d.effective_to > ${_next(params, as_of)}))",
        ]
        if project_key:
            # fact_subjects.project_key is the per-subject project tag; NULL-tagged
            # subjects are excluded so legacy data never leaks into a project answer.
            where.append(f"fs.project_key = ${_next(params, project_key)}")
        for f in spec.get("filters") or []:
            where.append(_filter_sql(source, f, params))
        sql += " WHERE " + " AND ".join(where)

    order_by = spec.get("order_by") or {}
    if order_by:
        col = order_by["field"]
        if source == "facts" and col in SEMANTIC_FACT_FIELDS:
            col = "f.value_num"
        elif source == "facts" and col == "subject_key":
            col = "fs.subject_key"
        else:
            col = f"f.{col}" if source == "facts" else col
        sql += f" ORDER BY {col} {ALLOWED_DIR[order_by['dir']]}"
    sql += f" LIMIT {_coerce_limit(spec.get('limit', DEFAULT_LIMIT))}"
    return sql, params


def _next(params: list[Any], v: Any) -> int:
    params.append(v)
    return len(params)


def _fold_query(text: str) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _is_exhaustive_query(query: str) -> bool:
    folded = _fold_query(query)
    return any(term in folded for term in _EXHAUSTIVE_QUERY_TERMS)


def _effective_limit(spec: dict, query: str) -> int:
    """Raise only exhaustive catalogue requests to the bounded full-catalog cap."""
    requested = int(spec.get("limit") or DEFAULT_LIMIT)
    if _is_exhaustive_query(query):
        return MAX_LIMIT
    return min(requested, MAX_LIMIT)


# match_semantics — range/approx semantics (plan §4.4 A8); pure, unit-testable.
def _row_value(row: dict, field_name: str) -> Any:
    if field_name in row:
        return row[field_name]
    if "value_num" in row and row.get("value_num") is not None:
        return row["value_num"]
    return row.get("value_text")


def _apply_cmp(v: Any, op: str, target: Any) -> bool:
    try:
        if op == "<":
            return v < target
        if op == "<=":
            return v <= target
        if op == ">":
            return v > target
        if op == ">=":
            return v >= target
    except TypeError:
        return False
    return False


def match_semantics(row: dict, field_name: str, op: str, value: Any) -> bool:
    """Apply an operator to one row with range/approx semantics; `in` is categorical only."""
    quality = row.get("quality")
    if quality in ("range", "approx"):
        rmin, rmax = row.get("range_min"), row.get("range_max")
        if op == "in":
            return False
        if op == "between":
            lo, hi = value
            return (rmin is None or rmin <= hi) and (rmax is None or rmax >= lo)
        if op == "<=":
            return rmin is not None and rmin <= value
        if op == "<":
            return rmin is not None and rmin < value
        if op == ">=":
            return rmax is not None and rmax >= value
        if op == ">":
            return rmax is not None and rmax > value
        return False  # '=' / '!=' never match a range

    v = _row_value(row, field_name)
    if v is None:
        return False
    if isinstance(value, (list, tuple)):  # between / in
        if op == "between":
            return _apply_cmp(v, ">=", value[0]) and _apply_cmp(v, "<=", value[1])
        if op == "in":
            return v in value
    if op == "=":
        return v == value
    if op == "!=":
        return v != value
    return _apply_cmp(v, op, value)


# FACT_EVIDENCE blocks (plan §4.4: fe-001..).
def _jsonable(v: Any) -> Any:
    """Coerce values to JSON-safe types: Decimal -> int/float, date -> ISO string."""
    if isinstance(v, Decimal):
        if v == v.to_integral_value():
            return int(v)
        return float(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


# fact_keys whose note must carry the unit explicitly — the LLM mixed "0đ" into a
# percent field once the unit was left implicit (round-2 defect D2/D3c).
_FACT_UNIT_NOTES: dict[str, tuple[str, str]] = {
    "deposit_pct": ("tỷ lệ vốn tự có, đơn vị %", "tỷ lệ % (NULL = chưa có, không phải 0%)"),
    "term_months": ("thời hạn, đơn vị tháng", "số tháng (NULL = chưa có, không phải 0)"),
    "area_m2": ("diện tích, đơn vị m²", "m² (NULL = chưa có)"),
    "price_vnd": ("số tiền, đơn vị đồng (VND)", "đồng (NULL = chưa có)"),
}

_INTEREST_NULL_NOTE = "lãi suất %/năm (NULL = chưa có, không phải 0%)"


def _is_zero_number(v: Any) -> bool:
    """True only for a genuine numeric zero (Decimal('0.0000') counts)."""
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float, Decimal)):
        return v == 0
    return False


def _positive_int(v: Any) -> int | None:
    """Coerce a duration fact to a positive whole-month count, else None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            iv = int(v)
        except (ValueError, OverflowError):
            return None
        return iv if iv > 0 else None
    return None


def _facts_note(row: dict, term_months: Any = None) -> str:
    """Provenance note for one fact row — value-aware (round-2 defect D2).

    The "NULL = chưa có" caveat must appear ONLY when the value is genuinely
    absent. HTLS and owner-funded support policies carry a real 0.0000 interest
    rate (data/_processed/business_rules.json, db/seed/policy_vay.sql), and
    labelling that as missing data made the facts panel read as machine garbage.
    A non-zero rate is reported without inventing a bank figure: the bank rate is
    decided per case, so the note only states the unit and the data source.

    A real 0% is a TIME-BOUNDED subsidy: the note only states "0%/năm" when the
    sibling term_months fact proves the duration, otherwise it defers to the
    policy term — an unbounded 0% claim would mislead the customer.
    """
    q = row.get("quality") or "exact"
    if q == "range":
        return f"khoảng {row.get('range_min')}–{row.get('range_max')} (dữ liệu range)"
    if q == "approx":
        return f"ước lượng ~{row.get('value_num')} (dữ liệu approx)"

    fact_key = row.get("fact_key")
    value = row.get("value_num")
    if value is None:
        value = row.get("value_text")

    if fact_key == "interest_rate_pct":
        if value is None:
            return _INTEREST_NULL_NOTE
        if _is_zero_number(value):
            label = policy_display(row.get("policy_key"))
            source = f"theo {label} của chủ đầu tư" if label else "theo chính sách của chủ đầu tư"
            term = _positive_int(term_months if term_months is not None else row.get("term_months"))
            if term is not None:
                return (
                    f"lãi suất ưu đãi 0%/năm trong {term} tháng đầu {source} "
                    "- mức hỗ trợ thật, không phải thiếu dữ liệu"
                )
            # No duration evidence on this row — a bare 0%/năm would read as
            # unlimited, so the note defers to the term written in the policy.
            return f"mức hỗ trợ lãi suất {source}, áp dụng theo thời hạn ghi trong chính sách"
        return (
            "lãi suất %/năm theo chính sách trong dữ liệu"
            " (lãi vay thực tế do ngân hàng quyết định từng case)"
        )

    unit_note = _FACT_UNIT_NOTES.get(str(fact_key))
    if unit_note:
        present_note, null_note = unit_note
        return present_note if value is not None else null_note
    return "số liệu gốc từ dữ liệu cấu trúc"


def build_fact_evidence(rows: list[dict], source: str, as_of: date | None) -> list[dict]:
    """Convert raw rows into FACT_EVIDENCE blocks (fe-001..) — the sole numeric source for generation."""  # noqa: E501
    # A 0% HTLS rate is time-bounded, but the duration lives in a SIBLING
    # term_months fact row for the same subject+policy, so index it up front
    # for the note builder instead of shipping an unbounded "0%/năm".
    term_by_scope: dict[tuple, Any] = {}
    if source != "v_unit_offers":
        for row in rows:
            if row.get("fact_key") == "term_months" and row.get("value_num") is not None:
                term_by_scope.setdefault(
                    (row.get("subject_key"), row.get("policy_key")), row.get("value_num")
                )
    fe: list[dict] = []
    for i, row in enumerate(rows, start=1):
        if source == "v_unit_offers":
            fields = {
                k: _jsonable(row.get(k))
                for k in OFFER_COLUMNS
                if k not in ("subject_id",) and row.get(k) is not None
            }
            note = (
                "derived: required_down_payment_vnd = CEIL(giá × deposit_pct/100); "
                "loan_amount_vnd = giá × (100 − deposit_pct)/100; monthly_principal_vnd = loan/term; "  # noqa: E501
                "monthly_interest_estimate_vnd = ước tính dư nợ gốc ban đầu (không phải lịch trả nợ)"  # noqa: E501
            )
            entry = {
                "fe_id": f"fe-{i:03d}",
                "subject": row.get("subject_key") or f"unit:{row.get('subject_id')}",
                # Additive legend (D4): machine keys stay byte-identical for
                # guard_output, the *_display fields give the LLM prose to quote.
                "subject_display": subject_display(
                    row.get("subject_key") or f"unit:{row.get('subject_id')}",
                    row.get("display_name"),
                ),
                "policy_key": row.get("policy_key"),
                "policy_display": policy_display(row.get("policy_key")),
                "fields": fields,
                "note": note,
                "quality": "exact",
                "effective_from": _jsonable(row.get("effective_from")),
                "effective_to": _jsonable(row.get("effective_to")),
                "source_doc_id": row.get("source_doc_id"),
                "campaign_key": row.get("campaign_key"),
                "fact_id": row.get("fact_id"),
            }
        else:
            value = row.get("value_num")
            if value is None:
                value = row.get("value_text")
            fields = {row.get("fact_key"): _jsonable(value)}
            entry = {
                "fe_id": f"fe-{i:03d}",
                "subject": row.get("subject_key"),
                "subject_display": subject_display(row.get("subject_key"), row.get("display_name")),
                "policy_key": row.get("policy_key"),
                "policy_display": policy_display(row.get("policy_key")),
                "fields": fields,
                "note": _facts_note(
                    row,
                    term_by_scope.get((row.get("subject_key"), row.get("policy_key"))),
                ),
                "quality": row.get("quality") or "exact",
                "trust_level": row.get("trust_level") or "confirmed",
                "range": {
                    "min": _jsonable(row.get("range_min")),
                    "max": _jsonable(row.get("range_max")),
                }
                if row.get("quality") in ("range", "approx")
                else None,
                "effective_from": _jsonable(row.get("effective_from")),
                "effective_to": _jsonable(row.get("effective_to")),
                "source_doc_id": row.get("source_doc_id"),
                "campaign_key": row.get("campaign_key"),
                "fact_id": row.get("fact_id"),
            }
        fe.append(entry)
    return fe


# Affordability leg (story 3.2) — deterministic numbers from v_unit_estimates only.
ESTIMATE_COLUMNS = (
    "subject_key",
    "display_name",
    "project_key",
    "attrs",
    "policy_key",
    "price_min_vnd",
    "price_max_vnd",
    "price_quality",
    "deposit_pct",
    "term_months",
    "interest_rate_pct",
)


async def _fetch_estimates(
    pool: asyncpg.Pool | None = None, project_key: str | None = None
) -> list[dict]:
    """Current v_unit_estimates rows — the view pins CURRENT_DATE, so no as_of.

    ``project_key`` (M8) filters in SQL so unrelated projects' rows are never
    shipped to the client; None keeps the unscoped read for legacy callers.
    """
    cols = ", ".join(ESTIMATE_COLUMNS)
    sql = f"SELECT {cols} FROM v_unit_estimates"
    params: list[str] = []
    if project_key:
        sql += " WHERE project_key = $1"
        params.append(project_key)
    async with with_rls_identity(pool=pool) as conn:
        recs = await conn.fetch(sql, *params)
    return [dict(r) for r in recs]


async def run_affordability(
    spec: dict, as_of: date | None, fetch=None, project_key: str | None = None
) -> SqlLegResult:
    """Deterministic affordability leg: estimates -> analyze -> fe evidence + meta.

    Numbers come only from v_unit_estimates via analyze_affordability (ADR-0002
    D2). The fetch is injectable so tests never touch the pool. budget_vnd below
    the 1M VND floor (FIX-3) is a spec violation -> degraded, never fabricated.
    ``project_key`` (story 10.4): estimates carry a project_key column, so the
    affordance analysis only sees rows of the requested project.
    """
    budget = spec.get("budget_vnd")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1_000_000:
        return SqlLegResult(
            [],
            {
                "mode": "affordability",
                "error": "budget_vnd invalid",
                "degraded_reason": "spec_invalid",
            },
            degraded=True,
        )
    try:
        if fetch is not None:
            # Injected fetch (tests) keeps the zero-argument contract; the
            # project filter below still applies to whatever it returns.
            rows = await fetch()
        elif project_key:
            rows = await _fetch_estimates(project_key=project_key)
        else:
            # Unscoped legacy call stays zero-argument: tests replace
            # _fetch_estimates with a bare ``async def fake():`` seam.
            rows = await _fetch_estimates()
        if project_key:
            # The estimates view spans every project; keep only the requested one
            # so a Soleil budget query never prices Camellia units (story 10.4).
            # The default DB fetch already applied the same predicate in SQL
            # (M8); this guard covers injected fetches and is a no-op otherwise.
            rows = [r for r in rows if r.get("project_key") == project_key]
        if not rows:
            # No estimates at all is a data problem, not an answer: degrade so
            # the pipeline falls back to RAG + audit instead of reporting
            # 'nothing affordable' (BH-9). Rows that exist but match nothing are
            # a legitimate 'nothing fits this budget' and stay non-degraded.
            return SqlLegResult(
                [],
                {
                    "mode": "affordability",
                    "source": "v_unit_estimates",
                    "degraded_reason": "no_estimates",
                },
                degraded=True,
            )
        # Skip rows without a price (F7 contract) — never fabricate a 0 offer.
        offers = [offer_from_row(r) for r in rows if r.get("price_min_vnd") is not None]
        # Deterministic cheapest-first order -> stable fe-001.. ids (EH-10).
        offers = sorted(offers, key=lambda o: o.price_min_vnd)
        result = analyze_affordability(offers, budget)
        evidence = affordability_rows(result)
        # Evidence bounded by the spec limit (default 20) — never unbounded.
        limit = int(spec.get("limit") or 20)
        if limit > 0:
            evidence = evidence[:limit]
    except Exception as exc:  # noqa: BLE001 — fetch/parse failure degrades, never crashes
        logger.warning("sql_leg: affordability data failed: %s", exc)
        return SqlLegResult([], {"mode": "affordability", "error": str(exc)}, degraded=True)
    audited_sql = f"SELECT {', '.join(ESTIMATE_COLUMNS)} FROM v_unit_estimates"
    if project_key:
        # Mirror the WHERE the default fetch pushed into SQL (M8) so the audit
        # records the query that actually ran.
        audited_sql += " WHERE project_key = $1"
    meta = {
        "mode": "affordability",
        "source": "v_unit_estimates",
        "row_count": len(evidence),
        "sql_query": audited_sql,
        # The view pins CURRENT_DATE (camellia_estimate.sql) — the requested
        # as_of is intentionally not applied; surface that to the audit (BH-16).
        "as_of_applied": False,
        **affordability_summary(result),
    }
    # Merge rule (workflow.py:357): any 'range'/'approx' quality OR
    # trust_level='estimate' caps confidence — fe rows are always estimate, so
    # has_approx must mirror the merge, not the quality-only summary (BH-10).
    meta["has_approx"] = any(
        e.get("quality") in ("range", "approx") or e.get("trust_level") == "estimate"
        for e in evidence
    )
    return SqlLegResult(evidence, meta, degraded=False)


# Runner.
async def run_sql_leg(
    spec: dict | None, as_of: date | None, query: str, project_key: str | None = None
) -> SqlLegResult:
    """Run R1 (spec-builder), R2 (nl2sql), or the affordability leg when routed.

    Any error/timeout returns a degraded SqlLegResult so the caller falls back to
    RAG-only instead of crashing.
    """
    if spec is None:
        return SqlLegResult([], {"mode": "none", "error": "no spec"}, degraded=False)

    # The router's default limit is intentionally small for ordinary answers,
    # but exhaustive catalogue/payment questions need every valid row. Keep the
    # override bounded and only activate it for explicit all-types wording.
    effective_spec = dict(spec)
    effective_spec["limit"] = _effective_limit(effective_spec, query)
    spec = effective_spec

    if spec.get("structured_path") == "nl2sql":
        try:
            from api.domain.services.nl2sql_guard import run_nl2sql  # noqa: PLC0415

            return await run_nl2sql(query, as_of)
        except Exception as exc:  # noqa: BLE001 — SQlnl2sqlError → degrade RAG-only + audit
            logger.warning("sql_leg: nl2sql degraded: %s", exc)
            return SqlLegResult([], {"mode": "nl2sql", "error": str(exc)}, degraded=True)

    # Affordability dispatch: v_unit_estimates is NOT in ALLOWED_SOURCES, so it
    # must run before validate_spec (mirrors nl2sql placement).
    if spec.get("structured_path") == "affordability":
        try:
            return await run_affordability(spec, as_of, project_key=project_key)
        except Exception as exc:  # noqa: BLE001 — leg failure degrades, never crashes
            logger.warning("sql_leg: affordability degraded: %s", exc)
            return SqlLegResult([], {"mode": "affordability", "error": str(exc)}, degraded=True)

    try:
        validate_spec(spec)
    except SpecError as exc:
        return SqlLegResult(
            [],
            {"mode": "spec", "error": f"spec invalid: {exc}", "degraded_reason": "spec_invalid"},
            degraded=True,
        )

    try:
        sql, params = build_sql(spec, as_of, project_key)
        rows: list[dict] = []
        async with with_rls_identity(timeout_s=1.5) as conn:
            recs = await conn.fetch(sql, *params)
            rows = [dict(r) for r in recs]

        # match_semantics in Python — exact range/approx semantics (SQL already returns a superset).
        for f in spec.get("filters") or []:
            rows = [r for r in rows if match_semantics(r, f["field"], f["op"], f["value"])]

        # the view has no subject_key — map from fact_subjects for display
        if spec["source"] == "v_unit_offers" and rows:
            ids = [r["subject_id"] for r in rows]
            async with with_rls_identity(timeout_s=1.5) as conn:
                recs = await conn.fetch(
                    "SELECT id, subject_key, display_name FROM fact_subjects WHERE id = ANY($1)",
                    ids,
                )
            keymap = {r["id"]: (r["subject_key"], r["display_name"]) for r in recs}
            for r in rows:
                sk, dn = keymap.get(r["subject_id"], (str(r["subject_id"]), None))
                r["subject_key"], r["display_name"] = sk, dn

        evidence = build_fact_evidence(rows, spec["source"], as_of)
        return SqlLegResult(
            evidence,
            {"mode": "spec", "source": spec["source"], "sql": sql, "row_count": len(evidence)},
            degraded=False,
        )
    except (asyncio.TimeoutError, asyncpg.PostgresError, SqlLegError) as exc:
        logger.warning("sql_leg: chạy R1 thất bại: %s", exc)
        return SqlLegResult([], {"mode": "spec", "error": str(exc)}, degraded=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("sql_leg: lỗi không mong đợi")
        return SqlLegResult([], {"mode": "spec", "error": str(exc)}, degraded=True)
