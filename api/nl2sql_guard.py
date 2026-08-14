"""NL2SQL route (R2) — sqlglot AST guard + read-only engine.

Protections: closed 2-table whitelist (v_unit_offers + campaigns), single-SELECT
AST validation, function allowlist, and a read-only pool with statement_timeout.
Any validation failure raises Sqlnl2sqlError; the caller degrades to rag-only.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

import asyncpg
import sqlglot
from sqlglot import exp

from api.constants import LLM_CALL_TIMEOUT_S
from api.dependencies import get_llm, model_for_role
from api.sql_leg import SqlLegResult, build_dsn, with_rls_identity

logger = logging.getLogger("api.nl2sql_guard")

ALLOWED_TABLES = frozenset({"v_unit_offers", "campaigns"})

# Function allowlist — blocks pg_sleep/set_config/pg_read_file and other system funcs.
ALLOWED_FUNCS = frozenset(
    {
        "count", "sum", "avg", "min", "max", "round", "ceil", "ceiling", "floor",
        "coalesce", "nullif", "cast", "date", "extract", "abs", "lower", "upper",
        "length", "concat", "now", "current_date", "current_timestamp",
        "trim", "substring", "to_char", "to_number", "distinct",
    }
)
MAX_WRAP_LIMIT = 20

_SCHEMA_PROMPT = """Bạn là chuyên gia SQL PostgreSQL. Chỉ được dùng 2 bảng sau (KHÔNG bảng khác):

Bảng v_unit_offers (căn hộ + chính sách vay):
  subject_id BIGINT, policy_key TEXT, price_vnd NUMERIC, deposit_pct NUMERIC,
  term_months INTEGER, interest_rate_pct NUMERIC, required_down_payment_vnd NUMERIC,
  loan_amount_vnd NUMERIC, monthly_principal_vnd NUMERIC, monthly_interest_estimate_vnd NUMERIC

Bảng campaigns (đợt mở bán):
  campaign_key TEXT, project_key TEXT, effective_from DATE, effective_to DATE,
  status TEXT, source_doc_id TEXT

QUY TẮC CỨNG:
1. Chỉ SELECT (aggregate COUNT/SUM/AVG/MIN/MAX + GROUP BY được phép). CẤM INSERT/UPDATE/DELETE/CREATE.
2. Không dấu chấm phẩy, không comment, không câu lệnh thứ hai.
3. Không dùng information_schema / pg_catalog / pg_* function.
4. Không dùng hàm bị cấm (pg_sleep, set_config, ...).
5. Trả về ĐÚNG MỘT câu SELECT, không kèm text khác, không fence markdown."""


class Sqlnl2sqlError(Exception):
    """Validation/execution failure — caller degrades to rag-only + audit."""


# sqlglot AST guard
def validate_sql(sql: str) -> str:
    """Validate and wrap generated SQL; raises Sqlnl2sqlError on any violation."""
    if not sql or not sql.strip():
        raise Sqlnl2sqlError("empty SQL")
    if re.search(r"(?is)(--|/\*.*?\*/)", sql):
        raise Sqlnl2sqlError("comment không được phép")
    if re.search(r"(?i)information_schema|pg_catalog", sql):
        raise Sqlnl2sqlError("truy cập information_schema/pg_catalog bị cấm")
    try:
        statements = sqlglot.parse(sql)
    except Exception as exc:  # noqa: BLE001
        raise Sqlnl2sqlError(f"parse error: {exc}") from exc
    if not statements or len(statements) != 1:
        raise Sqlnl2sqlError("phải là ĐÚNG 1 câu SELECT (không multi-statement)")

    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise Sqlnl2sqlError("chỉ SELECT được phép")

    for node in stmt.find_all(
        exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop,
        exp.Alter, exp.Merge, exp.TruncateTable
    ):
        raise Sqlnl2sqlError(f"DML/DDL không được phép: {node.sql()[:60]}")

    tables = {t.name.lower() for t in stmt.find_all(exp.Table)}
    if not tables:
        raise Sqlnl2sqlError("thiếu bảng FROM")
    forbidden = tables - ALLOWED_TABLES
    if forbidden:
        raise Sqlnl2sqlError(f"bảng ngoài whitelist: {sorted(forbidden)}")

    for f in stmt.find_all(exp.Func):
        name = (f.sql_name() or "").lower().split(".")[-1]
        if name not in ALLOWED_FUNCS:
            raise Sqlnl2sqlError(f"hàm bị cấm: {name}")

    return f"SELECT * FROM (\n{sql}\n) AS _q LIMIT {MAX_WRAP_LIMIT}"


def _as_of_param(params: list[Any], value: date) -> exp.Parameter:
    """Append a bind value and return its $n placeholder for the generated SQL."""
    params.append(value)
    return exp.Parameter(this=exp.Literal.number(len(params)))


def _add_campaign_interval(inner: exp.Select, params: list[Any], as_of_val: date) -> None:
    """Merge interval + status predicates into the query touching campaigns."""
    cond = exp.and_(
        exp.column("status").eq(exp.Literal.string("active")),
        exp.and_(
            exp.column("effective_from") <= _as_of_param(params, as_of_val),
            exp.or_(
                exp.column("effective_to").is_(None),
                exp.column("effective_to") > _as_of_param(params, as_of_val),
            ),
        ),
    )
    existing = inner.args.get("where")
    if existing is None:
        inner.set("where", exp.Where(this=cond))
    else:
        inner.set("where", exp.Where(this=exp.and_(existing.this, cond)))


def _enforce_as_of(wrapped_sql: str, as_of: date | None) -> tuple[str, list[Any]]:
    """Bind as_of in code (not LLM-instructed): route v_unit_offers through the
    parameterized function and add campaigns interval + status predicates.

    Returns the re-rendered wrapped SQL plus ordered bind params.
    """
    stmt = sqlglot.parse_one(wrapped_sql, read="postgres")
    params: list[Any] = []
    as_of_val = as_of or date.today()
    seen: set[str] = set()
    for table in list(stmt.find_all(exp.Table)):
        name = table.name.lower()
        if name not in ALLOWED_TABLES or name in seen:
            continue
        seen.add(name)
        if name == "v_unit_offers":
            func = exp.func("v_unit_offers_as_of", _as_of_param(params, as_of_val))
            table.replace(exp.alias_(func, table.alias or table.name))
        elif name == "campaigns":
            inner = table.find_ancestor(exp.Select)
            if inner is not None:
                _add_campaign_interval(inner, params, as_of_val)
    return stmt.sql(dialect="postgres"), params


def extract_sql(raw: str) -> str:
    """Strip markdown fences and find the first SELECT statement from LLM output."""
    t = (raw or "").strip()
    t = re.sub(r"^```(?:sql)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"(?is)(?<![;\s])select\b.*", t)
    if m:
        return m.group(0).strip().rstrip(";").strip()
    return t


# Read-only engine ro_nl2sql (read-only + 8s timeout)
_nl2sql_pool: asyncpg.Pool | None = None


async def get_nl2sql_pool() -> asyncpg.Pool:
    global _nl2sql_pool
    if _nl2sql_pool is None or _nl2sql_pool.is_closing():
        _nl2sql_pool = await asyncpg.create_pool(
            build_dsn(),
            min_size=1,
            max_size=3,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": "8000",
            },
        )
    return _nl2sql_pool


async def close_nl2sql_pool() -> None:
    global _nl2sql_pool
    if _nl2sql_pool is not None and not _nl2sql_pool.is_closing():
        await _nl2sql_pool.close()
    _nl2sql_pool = None


def _build_messages(query: str, as_of: date | None) -> list[dict]:
    as_of_txt = as_of.isoformat() if as_of else "hôm nay (CURRENT_DATE)"
    user = (
        f"Nhiệm vụ: trả về SQL SELECT trả lời câu hỏi sau (as_of ngày hiệu lực = {as_of_txt}):\n"
        f"{query}\n\n"
        "Chỉ SELECT; nếu cần lọc theo hiệu lực dùng effective_from/effective_to so với ngày hiệu lực."
    )
    return [
        {"role": "system", "content": _SCHEMA_PROMPT},
        {"role": "user", "content": user},
    ]


def _jsonable_row(v: Any) -> Any:
    from api.sql_leg import _jsonable  # noqa: PLC0415

    return _jsonable(v)


async def run_nl2sql(query: str, as_of: date | None) -> SqlLegResult:
    """LLM -> SQL -> AST guard -> wrap -> execute on ro_nl2sql; raises on failure."""
    model = model_for_role("nl2sql")
    raw = await get_llm().complete(_build_messages(query, as_of), model=model, timeout=LLM_CALL_TIMEOUT_S)
    sql = extract_sql(raw)
    if not sql:
        raise Sqlnl2sqlError("LLM không trả về SQL")
    wrapped = validate_sql(sql)
    wrapped, params = _enforce_as_of(wrapped, as_of)

    pool = await get_nl2sql_pool()
    rows: list[dict] = []
    async with with_rls_identity(timeout_s=8.0, pool=pool) as conn:
        recs = await conn.fetch(wrapped, *params)
        rows = [_jsonable_row(dict(r)) for r in recs]

    return SqlLegResult(
        [{"fe_id": f"fe-{i:03d}", "fields": r, "note": "kết quả aggregate/compare (NL2SQL)", "source": "nl2sql"} for i, r in enumerate(rows, start=1)],
        {"mode": "nl2sql", "sql_query": sql, "row_count": len(rows)},
        degraded=False,
    )
