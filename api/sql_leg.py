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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, AsyncIterator

import asyncpg

from api import get_cfg

logger = logging.getLogger("api.sql_leg")

# Closed-set allowlist (plan §4.4 — validated before building).
ALLOWED_SOURCES = ("facts", "v_unit_offers")

ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "v_unit_offers": (
        "subject_id", "policy_key", "price_vnd", "deposit_pct", "term_months",
        "interest_rate_pct", "required_down_payment_vnd", "loan_amount_vnd",
        "monthly_principal_vnd", "monthly_interest_estimate_vnd",
    ),
    "facts": (
        "price_vnd", "area_m2", "deposit_pct", "term_months", "interest_rate_pct",
        "subject_key", "subject_type", "value_num", "value_text", "unit", "quality",
        "policy_key", "campaign_key",
    ),
}

# Semantic fact_key fields on 'facts' — filtered by value_num/range.
SEMANTIC_FACT_FIELDS = {"price_vnd", "area_m2", "deposit_pct", "term_months", "interest_rate_pct"}

ALLOWED_OPS = ("=", "!=", "<", "<=", ">", ">=", "between", "in")
ALLOWED_DIR = {"asc": "ASC", "desc": "DESC"}
MIN_LIMIT, MAX_LIMIT, DEFAULT_LIMIT = 1, 20, 10

OFFER_COLUMNS = (
    "subject_id", "policy_key", "price_vnd", "deposit_pct", "term_months",
    "interest_rate_pct", "required_down_payment_vnd", "loan_amount_vnd",
    "monthly_principal_vnd", "monthly_interest_estimate_vnd",
)


class SpecError(ValueError):
    """Spec violates the closed set — caller degrades to RAG-only (§4.4)."""


class SqlLegError(Exception):
    """SQL leg execution failure (timeout/DB) — caller degrades."""


@dataclass
class SqlLegResult:
    rows: list[dict] = field(default_factory=list)  # FACT_EVIDENCE blocks (fe-...)
    meta: dict = field(default_factory=dict)        # {mode, source, sql, sql_query, row_count, error}
    degraded: bool = False


# RO pool — owner connects; SET LOCAL ROLE ro_query runs inside each transaction.
_ro_pool: asyncpg.Pool | None = None


def build_dsn() -> str:
    """Build the DSN from Settings (no hardcoding)."""
    host = get_cfg("postgres_host", "localhost")
    port = get_cfg("postgres_port", 5432)
    user = get_cfg("postgres_user", "ragre")
    password = get_cfg("postgres_password", "")
    db = get_cfg("postgres_database", "ragre")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


async def get_ro_pool() -> asyncpg.Pool:
    """Lazy singleton RO pool; role switching happens inside each transaction."""
    global _ro_pool
    if _ro_pool is None or _ro_pool.is_closed():
        _ro_pool = await asyncpg.create_pool(
            build_dsn(), min_size=1, max_size=int(get_cfg("postgres_max_connections", 5) or 5),
        )
    return _ro_pool


async def close_ro_pool() -> None:
    global _ro_pool
    if _ro_pool is not None and not _ro_pool.is_closed():
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
    pool = pool or await get_ro_pool()
    conn: asyncpg.Connection = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(f"SET LOCAL statement_timeout = '{int(timeout_s * 1000)}ms'")
        await conn.execute(f"SET LOCAL ROLE {role}")
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


def build_sql(spec: dict, as_of: date | None) -> tuple[str, list[Any]]:
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
        for f in spec.get("filters") or []:
            where.append(_filter_sql(source, f, params))
        if where:
            sql += " WHERE " + " AND ".join(where)
    else:
        # facts: join fact_subjects; enforce interval validity at as_of and a published
        # doc (defense-in-depth; RLS FORCE still guards if ever forgotten).
        sql = (
            "SELECT f.id AS fact_id, fs.subject_key, fs.subject_type, fs.display_name, "
            "f.fact_key, f.policy_key, f.campaign_key, f.value_num, f.value_text, f.unit, f.quality, "
            "f.range_min, f.range_max, f.effective_from, f.effective_to, "
            "f.source_doc_id, f.source_chunk_id "
            "FROM facts f JOIN fact_subjects fs ON fs.id = f.subject_id"
        )
        where = [f"f.effective_from <= ${_next(params, as_of)}",
                 f"(f.effective_to IS NULL OR f.effective_to > ${_next(params, as_of)})",
                 f"EXISTS (SELECT 1 FROM documents d WHERE d.doc_id = f.source_doc_id "
                 f"AND d.status = 'published' AND d.effective_from <= ${_next(params, as_of)} "
                 f"AND (d.effective_to IS NULL OR d.effective_to > ${_next(params, as_of)}))"]
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


def _facts_note(row: dict) -> str:
    q = row.get("quality") or "exact"
    if q == "range":
        return f"khoảng {row.get('range_min')}–{row.get('range_max')} (dữ liệu range)"
    if q == "approx":
        return f"ước lượng ~{row.get('value_num')} (dữ liệu approx)"
    if row.get("fact_key") == "interest_rate_pct":
        return "lãi suất %/năm (NULL = chưa có, không phải 0%)"
    return "số liệu gốc từ dữ liệu cấu trúc"


def build_fact_evidence(rows: list[dict], source: str, as_of: date | None) -> list[dict]:
    """Convert raw rows into FACT_EVIDENCE blocks (fe-001..) — the sole numeric source for generation."""
    fe: list[dict] = []
    for i, row in enumerate(rows, start=1):
        if source == "v_unit_offers":
            fields = {k: _jsonable(row.get(k)) for k in OFFER_COLUMNS if k not in ("subject_id",) and row.get(k) is not None}
            note = (
                "derived: required_down_payment_vnd = CEIL(giá × deposit_pct/100); "
                "loan_amount_vnd = giá × (100 − deposit_pct)/100; monthly_principal_vnd = loan/term; "
                "monthly_interest_estimate_vnd = ước tính dư nợ gốc ban đầu (không phải lịch trả nợ)"
            )
            entry = {
                "fe_id": f"fe-{i:03d}",
                "subject": row.get("subject_key") or f"unit:{row.get('subject_id')}",
                "policy_key": row.get("policy_key"),
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
                "policy_key": row.get("policy_key"),
                "fields": fields,
                "note": _facts_note(row),
                "quality": row.get("quality") or "exact",
                "range": {"min": _jsonable(row.get("range_min")), "max": _jsonable(row.get("range_max"))}
                if row.get("quality") in ("range", "approx") else None,
                "effective_from": _jsonable(row.get("effective_from")),
                "effective_to": _jsonable(row.get("effective_to")),
                "source_doc_id": row.get("source_doc_id"),
                "campaign_key": row.get("campaign_key"),
                "fact_id": row.get("fact_id"),
            }
        fe.append(entry)
    return fe


# Runner.
async def run_sql_leg(spec: dict | None, as_of: date | None, query: str) -> SqlLegResult:
    """Run R1 (spec-builder) or R2 (nl2sql) when structured_path == 'nl2sql'.

    Any error/timeout returns a degraded SqlLegResult so the caller falls back to
    RAG-only instead of crashing.
    """
    if spec is None:
        return SqlLegResult([], {"mode": "none", "error": "no spec"}, degraded=False)

    if spec.get("structured_path") == "nl2sql":
        try:
            from api.nl2sql_guard import run_nl2sql  # noqa: PLC0415

            return await run_nl2sql(query, as_of)
        except Exception as exc:  # noqa: BLE001 — SQlnl2sqlError → degrade RAG-only + audit
            logger.warning("sql_leg: nl2sql degraded: %s", exc)
            return SqlLegResult([], {"mode": "nl2sql", "error": str(exc)}, degraded=True)

    try:
        validate_spec(spec)
    except SpecError as exc:
        return SqlLegResult([], {"mode": "spec", "error": f"spec invalid: {exc}", "degraded_reason": "spec_invalid"}, degraded=True)

    try:
        sql, params = build_sql(spec, as_of)
        rows: list[dict] = []
        async with with_rls_identity(timeout_s=2.0) as conn:
            recs = await conn.fetch(sql, *params)
            rows = [dict(r) for r in recs]

        # match_semantics in Python — exact range/approx semantics (SQL already returns a superset).
        for f in spec.get("filters") or []:
            rows = [r for r in rows if match_semantics(r, f["field"], f["op"], f["value"])]

        # the view has no subject_key — map from fact_subjects for display
        if spec["source"] == "v_unit_offers" and rows:
            ids = [r["subject_id"] for r in rows]
            async with with_rls_identity(timeout_s=2.0) as conn:
                recs = await conn.fetch(
                    "SELECT id, subject_key, display_name FROM fact_subjects WHERE id = ANY($1)", ids
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
