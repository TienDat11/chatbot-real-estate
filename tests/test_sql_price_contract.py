"""P0-04 — SQL metadata + price selection.

Two layers:

1. Offline (always runs): the route seam strips ONE literal ``project_name``
   only when the trusted active catalogue names THIS project, folds a top-level
   facts ``subject_type`` into one bound filter, and refuses every ambiguous
   metadata shape before a pool is acquired.
2. Disposable PostgreSQL (real evaluation, never a fake fetch): nine low
   ``interest_rate_pct`` rows, one ``term_months`` row and one current
   exact/confirmed/same-project ``price_vnd``. A price-ranked spec must return
   the price and must not let a rate or a term take a rank; the executed SQL and
   its params are captured to prove the whole price predicate sits in WHERE,
   ahead of ``ORDER BY ... LIMIT``. Every lineage/currency negative is checked
   against ``CURRENT_DATE`` read from that same database.

The DB layer needs an explicitly configured LOCAL disposable server::

    P004_TEST_PG_ADMIN=postgresql://ragre:pass@127.0.0.1:5432/postgres

Pooler/production-looking hosts are refused outright: this fixture creates and
drops a database and a role, which must never happen on a shared server.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

import re

import pytest

import api.application.pipelines.workflow as workflow_module
import api.application.services.sql_leg as sql_leg
import api.domain.services.rewrite as rewrite_module
from api.application.pipelines.workflow import (
    GuardedEv,
    RagQueryWorkflow,
    SqlRequestEv,
    _exact_unit_price_spec,
)
from api.application.services.project_context import ProjectContext
from api.application.services.sql_leg import (
    SpecError,
    SqlLegResult,
    build_sql,
    run_sql_leg,
    validate_spec,
)
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.rewrite import RoutedResult
from ingest.subject_key import normalize_subject_key

REPO = Path(__file__).resolve().parents[1]
SCHEMA_SQL = REPO / "db" / "schema.sql"

PROJECT = "camellia"
OTHER_PROJECT = "soleil"
CATALOGUE = ((PROJECT, "The Camellia"), (OTHER_PROJECT, "Soleil"))
CONFIRMED_PRICE = 2_850_000_000
SAME_AMOUNT_OTHER_UNIT = 2_850_000_000


class _Awaitable:
    """The state store is read with ``await``; the flag edit path is sync."""

    __slots__ = ("_value",)

    def __init__(self, value) -> None:
        self._value = value

    def __await__(self):
        if False:  # pragma: no cover — makes this a generator
            yield
        return self._value


class _Store:
    """The slice of llama_index DictState the route and sql_leg steps touch."""

    def __init__(self, data: dict) -> None:
        self._data = data
        self.raw = data

    def get(self, key: str, default=None):
        return _Awaitable(self._data.get(key, default))

    def set(self, key: str, value):
        self._data[key] = value
        return _Awaitable(None)

    def edit_state(self):
        store = self

        class _Edit:
            async def __aenter__(self):
                return store._data

            async def __aexit__(self, *exc):
                return None

        return _Edit()


class _Ctx:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.events: list[object] = []

    def send_event(self, event) -> None:
        self.events.append(event)


def _routed(spec, *, structured_path: str = "spec", query: str = "") -> RoutedResult:
    return RoutedResult(
        rewritten=query,
        routing={
            "needs_rag": True,
            "needs_sql": True,
            "structured_path": structured_path,
            "needs_geo": False,
        },
        sql_spec=spec,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=False,
        as_of=None,
    )


def _store(project_key: str = PROJECT, catalogue=CATALOGUE) -> _Store:
    return _Store(
        {
            "history": [],
            "as_of_date": None,
            "audit": {},
            "project_context": ProjectContext(project_key, "normal", catalogue),
        }
    )


def _drive_route(monkeypatch, query: str, spec, *, project_key: str = PROJECT, ctx=None):
    """Run the real route AND sql_leg steps with a fake router; return (ctx, calls)."""
    calls: list[tuple] = []

    async def fake_rewrite(clean, history, as_of_iso):
        return _routed(spec, query=clean)

    async def fake_guard_input(raw):
        return InputGuardResult(clean=raw, rejected=False, degraded=False)

    async def fake_run_sql_leg(sql_spec, as_of, clean_query, bound_project):
        calls.append((sql_spec, as_of, clean_query, bound_project))
        return SqlLegResult([], {"mode": "none"}, degraded=False)

    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "guard_input", fake_guard_input)
    monkeypatch.setattr(workflow_module, "run_sql_leg", fake_run_sql_leg)

    ctx = ctx or _Ctx(_store(project_key))
    ctx.store.set("guard", InputGuardResult(clean=query, rejected=False, degraded=False))

    async def go():
        workflow = RagQueryWorkflow()
        await workflow.route(ctx, GuardedEv(clean=query))
        await workflow.sql_leg(ctx, SqlRequestEv())

    asyncio.run(go())
    return ctx, calls


# --- A. route metadata -------------------------------------------------------


def _facts_spec_with_metadata(value, op: str = "=", *, extra=()) -> dict:
    return {
        "source": "facts",
        "filters": [{"field": "project_name", "op": op, "value": value}, *extra],
        "limit": 10,
    }


def test_route_strips_one_literal_project_name_key(monkeypatch):
    """The selected KEY named as metadata is dropped; every other filter stays."""
    original = _facts_spec_with_metadata(
        " CAMELLIA ", extra=[{"field": "area_m2", "op": ">", "value": 40}]
    )
    _ctx, calls = _drive_route(monkeypatch, "căn nào diện tích lớn", original)

    assert calls, "SQL must still run for a spec whose metadata was recognized"
    spec, _as_of, _query, bound_project = calls[0]
    assert spec["filters"] == [{"field": "area_m2", "op": ">", "value": 40}]
    # Server-owned scope: the bound key is the context key, never the model's text.
    assert bound_project == PROJECT
    # The model's dict is read, never rewritten in place.
    assert original["filters"][0] == {"field": "project_name", "op": "=", "value": " CAMELLIA "}


def test_route_strips_literal_ilike_display_name(monkeypatch):
    """A pattern-free ilike naming the unique display of THIS project is metadata."""
    _ctx, calls = _drive_route(
        monkeypatch, "giá bao nhiêu", _facts_spec_with_metadata("The Camellia", "ilike")
    )
    assert calls[0][0]["filters"] == []


@pytest.mark.parametrize(
    ("value", "op"),
    [
        ("%Camellia%", "="),
        ("%Camellia%", "ilike"),
        ("C_mellia", "ilike"),
        ("Camel\\lia", "ilike"),
        ("soleil", "="),
        ("Soleil", "="),
        ("   ", "="),
        (42, "="),
        (None, "="),
    ],
)
def test_route_rejects_untrusted_project_metadata(monkeypatch, value, op):
    ctx, calls = _drive_route(
        monkeypatch, "giá bao nhiêu", _facts_spec_with_metadata(value, op)
    )
    assert calls == [], "no SQL leg may run for unresolvable identity metadata"
    routed = ctx.store.raw["routed"]
    assert routed.routing["needs_sql"] is False
    assert "spec_invalid" in ctx.store.raw.get("degraded", [])
    # The rejected metadata stays auditable even though the route is closed.
    assert ctx.store.raw["audit"]["sql_spec"]["filters"][0]["value"] == value


def test_route_rejects_duplicate_project_name_filters(monkeypatch):
    spec = _facts_spec_with_metadata(
        "camellia", extra=[{"field": "project_name", "op": "=", "value": "camellia"}]
    )
    _ctx, calls = _drive_route(monkeypatch, "giá bao nhiêu", spec)
    assert calls == []


@pytest.mark.parametrize(
    ("catalogue", "value"),
    [
        # The display name is owned by two active entries.
        ((("camellia", "The Camellia"), ("camellia_alt", "The Camellia")), "The Camellia"),
        # The value is this project's key AND another project's display name.
        ((("camellia", "Riverfront"), ("soleil", "camellia")), "camellia"),
        # A name that is only another project's entry.
        ((("camellia", "The Camellia"), ("soleil", "Soleil")), "Soleil"),
    ],
)
def test_route_rejects_ambiguous_or_foreign_catalogue_names(monkeypatch, catalogue, value):
    ctx, calls = _drive_route(
        monkeypatch,
        "giá bao nhiêu",
        _facts_spec_with_metadata(value),
        ctx=_Ctx(_store(PROJECT, catalogue)),
    )
    assert calls == [], "an ambiguous or foreign identity is never resolved by guessing"
    assert ctx.store.raw["routed"].routing["needs_sql"] is False


def test_validate_spec_never_accepts_project_name():
    """Even a trusted-looking literal is outside the SQL field allowlist."""
    with pytest.raises(SpecError):
        validate_spec(_facts_spec_with_metadata("camellia"))


# --- B. top-level subject_type ----------------------------------------------


def test_top_level_subject_type_becomes_one_bound_filter():
    spec = {"source": "facts", "subject_type": " unit ", "filters": [], "limit": 5}
    normalized = validate_spec(spec)

    assert normalized is not spec
    assert "subject_type" not in normalized
    assert normalized["filters"] == [{"field": "subject_type", "op": "=", "value": "unit"}]
    assert spec["subject_type"] == " unit ", "the model's dict is never mutated"

    sql, params = build_sql(normalized, date(2026, 8, 1), project_key=PROJECT)
    assert sql.count("fs.subject_type = $") == 1
    assert "unit" in params


@pytest.mark.parametrize("value", ["", "   ", None, 7, ["unit"]])
def test_top_level_subject_type_rejects_non_string_or_blank(value):
    with pytest.raises(SpecError):
        validate_spec({"source": "facts", "subject_type": value, "filters": []})


def test_top_level_subject_type_conflicting_with_filter_is_ambiguous():
    spec = {
        "source": "facts",
        "subject_type": "unit",
        "filters": [{"field": "subject_type", "op": "=", "value": "project"}],
    }
    with pytest.raises(SpecError):
        validate_spec(spec)


def test_subject_type_filter_without_top_level_key_is_left_alone():
    spec = {
        "source": "facts",
        "filters": [{"field": "subject_type", "op": "=", "value": "unit"}],
    }
    assert validate_spec(spec)["filters"] == spec["filters"]


def test_direct_builder_folds_top_level_subject_type():
    sql, params = build_sql(
        {"source": "facts", "subject_type": "unit", "filters": [], "limit": 3},
        date(2026, 8, 1),
        project_key=PROJECT,
    )
    slot = re.search(r"fs\.subject_type = \$(\d+)", sql)
    assert slot is not None
    assert params[int(slot.group(1)) - 1] == "unit"


def test_offers_spec_keeps_its_own_top_level_subject_type():
    """v_unit_offers is not a facts spec; the affordability path owns that key."""
    spec = {"source": "v_unit_offers", "subject_type": "unit", "filters": []}
    assert validate_spec(spec) == spec


def test_run_sql_leg_degrades_before_fetch_on_ambiguous_subject_type(monkeypatch):
    touched: list[tuple] = []

    async def exploding_pool():
        touched.append(("pool",))
        raise AssertionError("pool must never be acquired for an invalid spec")

    monkeypatch.setattr(sql_leg, "get_ro_pool", exploding_pool)
    result = asyncio.run(
        run_sql_leg(
            {"source": "facts", "subject_type": "", "filters": [], "limit": 5},
            None,
            "giá bao nhiêu",
            PROJECT,
        )
    )
    assert result.degraded is True
    assert result.meta["degraded_reason"] == "spec_invalid"
    assert touched == []


# --- C/D. disposable PostgreSQL ---------------------------------------------


def _admin_dsn() -> str:
    dsn = os.environ.get("P004_TEST_PG_ADMIN", "").strip()
    if not dsn:
        pytest.skip(
            "P004_TEST_PG_ADMIN is unset — the P0-04 binary gate needs a disposable "
            "local PostgreSQL (see this module docstring)"
        )
    host = (urlparse(dsn).hostname or "").lower()
    if "pooler" in host or "supabase" in host or "rds" in host:
        pytest.fail(f"refusing a shared/production PostgreSQL target: {host}")
    return dsn


def _dsn_for(admin_dsn: str, dbname: str) -> str:
    return urlparse(admin_dsn)._replace(path=f"/{dbname}").geturl()


# doc_id, project_key, status, effective_from offset (days), effective_to offset
_DOCS = [
    ("p004-price", PROJECT, "published", -400, None),
    ("p004-price-expired", PROJECT, "published", -400, -1),
    ("p004-price-deprecated", PROJECT, "deprecated", -400, None),
    ("p004-soleil", OTHER_PROJECT, "published", -400, None),
    ("p004-untagged", None, "published", -400, None),
]

# campaign_key, project, from offset, to offset, source doc, status
_CAMPAIGNS = [
    ("p004-cam", PROJECT, -400, None, "p004-price", "active"),
    ("p004-cam-expired", PROJECT, -400, -1, "p004-price", "active"),
    ("p004-cam-inactive", PROJECT, -400, None, "p004-price", "expired"),
    ("p004-cam-deprecated-src", PROJECT, -400, None, "p004-price-deprecated", "active"),
    ("p004-cam-foreign-src", PROJECT, -400, None, "p004-soleil", "active"),
    ("p004-cam-untagged-src", PROJECT, -400, None, "p004-untagged", "active"),
    ("p004-cam-foreign", OTHER_PROJECT, -400, None, "p004-soleil", "active"),
]

_RATE_POLICIES = tuple(f"bank_{i}" for i in range(1, 10))

# subject_key -> (subject_type, project_key, fact overrides). Every negative
# carries its own figure, so one ranked query proves all of them are excluded.
_NEGATIVES: dict[str, tuple[str, str | None, dict]] = {
    "unit:camellia/neg-wrong-unit": ("unit", PROJECT, {"unit": "m2"}),
    "unit:camellia/neg-range": (
        "unit",
        PROJECT,
        {
            "quality": "range",
            "trust": "estimate",
            "value": None,
            "range_min": 8_000_002,
            "range_max": 8_000_009,
        },
    ),
    "unit:camellia/neg-estimate": ("unit", PROJECT, {"trust": "estimate"}),
    "unit:camellia/neg-fact-expired": ("unit", PROJECT, {"to_offset": -1}),
    "unit:camellia/neg-fact-ends-today": ("unit", PROJECT, {"to_offset": 0}),
    "unit:camellia/neg-doc-expired": ("unit", PROJECT, {"doc": "p004-price-expired"}),
    "unit:camellia/neg-doc-deprecated": ("unit", PROJECT, {"doc": "p004-price-deprecated"}),
    "unit:camellia/neg-doc-foreign": ("unit", PROJECT, {"doc": "p004-soleil"}),
    "unit:camellia/neg-campaign-expired": ("unit", PROJECT, {"campaign": "p004-cam-expired"}),
    "unit:camellia/neg-campaign-inactive": ("unit", PROJECT, {"campaign": "p004-cam-inactive"}),
    "unit:camellia/neg-campaign-deprecated-src": (
        "unit",
        PROJECT,
        {"campaign": "p004-cam-deprecated-src"},
    ),
    "unit:camellia/neg-campaign-foreign-src": (
        "unit",
        PROJECT,
        {"campaign": "p004-cam-foreign-src"},
    ),
    "unit:camellia/neg-campaign-untagged-src": (
        "unit",
        PROJECT,
        {"campaign": "p004-cam-untagged-src"},
    ),
    "unit:camellia/neg-no-campaign": ("unit", PROJECT, {"campaign": None}),
    "unit:camellia/neg-null-value": ("unit", PROJECT, {"value": None}),
    "unit:camellia/neg-untagged-subject": ("unit", None, {}),
}

_NEGATIVE_BASE_VALUE = 8_000_000


class _RecordingConnection:
    """Delegates to a REAL asyncpg connection, recording only what is executed."""

    def __init__(self, conn, log: list[tuple[str, list]]) -> None:
        self._conn = conn
        self._log = log

    def transaction(self):
        return self._conn.transaction()

    async def execute(self, sql, *args):
        return await self._conn.execute(sql, *args)

    async def fetch(self, sql, *args):
        self._log.append((sql, list(args)))
        return await self._conn.fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        self._log.append((sql, list(args)))
        return await self._conn.fetchrow(sql, *args)


class _RecordingPool:
    def __init__(self, pool, log: list[tuple[str, list]]) -> None:
        self._pool = pool
        self._log = log

    async def acquire(self):
        return _RecordingConnection(await self._pool.acquire(), self._log)

    async def release(self, conn) -> None:
        await self._pool.release(conn._conn)


def _seed(cur, today: date) -> None:
    def offset(days):
        return today + timedelta(days=days)

    for doc_id, project, status, start, end in _DOCS:
        cur.execute(
            "INSERT INTO documents (doc_id, kind, title, source_file, effective_from, "
            "effective_to, status, content_hash, project_key) "
            "VALUES (%s, 'price', %s, 'p004-fixture', %s, %s, %s, %s, %s)",
            (
                doc_id,
                doc_id,
                offset(start),
                offset(end) if end is not None else None,
                status,
                f"hash-{doc_id}",
                project,
            ),
        )
    for key, project, start, end, doc, status in _CAMPAIGNS:
        cur.execute(
            "INSERT INTO campaigns (campaign_key, project_key, effective_from, effective_to, "
            "source_doc_id, status) VALUES (%s, %s, %s, %s, %s, %s)",
            (key, project, offset(start), offset(end) if end is not None else None, doc, status),
        )

    subjects = [
        ("unit:camellia/CH-03", "unit", PROJECT, "Căn CH-03"),
        ("unit:camellia/CH-11", "unit", PROJECT, "Căn CH-11"),
        ("unit:camellia/CH-13A", "unit", PROJECT, "Căn CH-13A"),
        ("unit:camellia/2pn-band", "unit", PROJECT, "Căn hộ 2PN (dải giá)"),
        ("project:camellia", "project", PROJECT, "Camellia"),
        ("unit:soleil/CH-03", "unit", OTHER_PROJECT, "Căn CH-03 Soleil"),
        *[(key, stype, proj, key) for key, (stype, proj, _) in _NEGATIVES.items()],
    ]
    for key, stype, proj, name in subjects:
        cur.execute(
            "INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key) "
            "VALUES (%s, %s, %s, %s)",
            (normalize_subject_key(key), stype, name, proj),
        )

    def add_fact(
        subject_key,
        fact_key,
        value,
        *,
        unit="vnd",
        quality="exact",
        trust="confirmed",
        policy="htls",
        campaign="p004-cam",
        doc="p004-price",
        range_min=None,
        range_max=None,
        to_offset=None,
        from_offset=-400,
    ):
        cur.execute(
            "INSERT INTO facts (subject_id, fact_key, policy_key, campaign_key, value_num, "
            "unit, quality, trust_level, range_min, range_max, effective_from, effective_to, "
            "source_doc_id) "
            "SELECT id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
            "FROM fact_subjects WHERE subject_key = %s",
            (
                fact_key,
                policy,
                campaign,
                value,
                unit,
                quality,
                trust,
                range_min,
                range_max,
                offset(from_offset),
                offset(to_offset) if to_offset is not None else None,
                doc,
                normalize_subject_key(subject_key),
            ),
        )

    # Confirmed current sale prices.
    add_fact("unit:camellia/CH-03", "price_vnd", CONFIRMED_PRICE)
    add_fact("unit:camellia/CH-11", "price_vnd", SAME_AMOUNT_OTHER_UNIT)
    # Labelled estimates only — never a confirmed price.
    add_fact(
        "unit:camellia/CH-13A",
        "price_vnd",
        None,
        quality="range",
        trust="estimate",
        range_min=1_980_000_000,
        range_max=2_640_000_000,
    )
    add_fact(
        "unit:camellia/2pn-band",
        "price_vnd",
        None,
        quality="range",
        trust="estimate",
        range_min=3_100_000_000,
        range_max=3_400_000_000,
    )
    # Foreign project: a real current confirmed price, in another scope.
    add_fact(
        "unit:soleil/CH-03",
        "price_vnd",
        9_900_000_000,
        campaign="p004-cam-foreign",
        doc="p004-soleil",
    )

    # Nine rates and one term: every value_num sorts far BELOW the price, so
    # without the eligibility predicate in WHERE they occupy every LIMIT slot.
    for index, policy in enumerate(_RATE_POLICIES, start=1):
        add_fact(
            "project:camellia",
            "interest_rate_pct",
            3 + index / 10,
            unit="pct",
            policy=policy,
        )
    add_fact("project:camellia", "term_months", 240, unit="months", policy=_RATE_POLICIES[0])

    for index, (key, (_stype, _proj, overrides)) in enumerate(_NEGATIVES.items()):
        overrides = dict(overrides)
        add_fact(
            key, "price_vnd", overrides.pop("value", _NEGATIVE_BASE_VALUE + index), **overrides
        )


def _drop_disposable(psycopg2, admin_dsn: str, dbname: str, app_role: str) -> None:
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        cur.execute(f"DROP ROLE IF EXISTS {app_role}")
    admin.close()


@pytest.fixture(scope="module")
def price_db():
    """A disposable database cloned from db/schema.sql on a LOCAL PostgreSQL."""
    pytest.importorskip("psycopg2")
    import psycopg2

    admin_dsn = _admin_dsn()
    dbname = "p004_price_contract"
    app_role = "p004_app"

    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{dbname}"')
        cur.execute(f"DROP ROLE IF EXISTS {app_role}")
        cur.execute(f"CREATE ROLE {app_role} LOGIN NOSUPERUSER PASSWORD 'p004_app_pw'")
    admin.close()

    conn = psycopg2.connect(_dsn_for(admin_dsn, dbname))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for extension in ("vector", "btree_gist", "pgcrypto"):
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            cur.execute("SELECT CURRENT_DATE")
            today = cur.fetchone()[0]
            _seed(cur, today)
            cur.execute(f"GRANT ro_query TO {app_role}")
            cur.execute("SELECT has_table_privilege('ro_query', 'facts', 'SELECT')")
            assert cur.fetchone()[0] is True, "ro_query grants must exist for this fixture"
            conn.close()
    except BaseException:
        conn.close()
        _drop_disposable(psycopg2, admin_dsn, dbname, app_role)
        raise

    yield {"dsn": _dsn_for(admin_dsn, dbname), "today": today}

    _drop_disposable(psycopg2, admin_dsn, dbname, app_role)


async def _pool(dsn: str):
    import asyncpg

    return await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=2,
        statement_cache_size=0,
        max_cached_statement_lifetime=0,
        max_cacheable_statement_size=0,
    )


def _run_on_db(monkeypatch, db: dict, spec: dict, *, as_of=None, query: str = ""):
    """Execute one spec on the REAL disposable database; return (result, captured)."""
    captured: list[tuple[str, list]] = []

    async def go():
        pool = await _pool(db["dsn"])
        recording = _RecordingPool(pool, captured)

        async def get_recording_pool():
            return recording

        monkeypatch.setattr(sql_leg, "get_ro_pool", get_recording_pool)
        try:
            return await run_sql_leg(spec, as_of, query, PROJECT)
        finally:
            await pool.close()

    return asyncio.run(go()), captured


def _ranked_price_spec() -> dict:
    return {"source": "facts", "order_by": {"field": "price_vnd", "dir": "asc"}, "limit": 10}


def _ranked(monkeypatch, db: dict, **kwargs):
    result, captured = _run_on_db(monkeypatch, db, _ranked_price_spec(), **kwargs)
    assert result.degraded is False, result.meta
    return result, captured


def test_ranked_price_returns_the_price_not_rates_or_terms(monkeypatch, price_db):
    """Nine rates + one term sort below the price; only the price takes a rank."""
    result, _captured = _ranked(monkeypatch, price_db)

    assert [row["subject"] for row in result.rows] == [
        "unit:camellia/ch03",
        "unit:camellia/ch11",
    ]
    assert [row["fields"]["price_vnd"] for row in result.rows] == [
        CONFIRMED_PRICE,
        SAME_AMOUNT_OTHER_UNIT,
    ]


def test_ranked_price_predicates_sit_in_where_before_order_and_limit(monkeypatch, price_db):
    """The eligibility predicate is evaluated BEFORE ORDER BY / LIMIT, fully bound."""
    _result, captured = _ranked(monkeypatch, price_db)
    sql, params = captured[-1]

    where_at = sql.index(" WHERE ")
    order_at = sql.index(" ORDER BY ")
    limit_at = sql.index(" LIMIT ")
    assert where_at < order_at < limit_at
    eligible = sql[where_at:order_at]
    for predicate in sql_leg.ELIGIBLE_PRICE_PREDICATES:
        assert predicate in eligible, predicate
    # Scope is bound, never interpolated: the project is a param, not SQL text.
    assert PROJECT not in sql
    assert params.count(PROJECT) == 1
    assert "ORDER BY f.value_num ASC" in sql
    assert sql.endswith("LIMIT 10")


def test_price_ranking_requires_a_resolved_project_scope():
    with pytest.raises(SpecError):
        build_sql(_ranked_price_spec(), date(2026, 8, 1))
    with pytest.raises(SpecError):
        build_sql(_ranked_price_spec(), date(2026, 8, 1), project_key="  ")


def test_rate_ordering_is_untouched_by_the_price_contract(monkeypatch, price_db):
    """Opposite control: a rate sort still returns every rate row."""
    result, _captured = _run_on_db(
        monkeypatch,
        price_db,
        {"source": "facts", "order_by": {"field": "interest_rate_pct", "dir": "asc"}, "limit": 20},
    )
    assert result.degraded is False
    rates = [
        row["fields"]["interest_rate_pct"]
        for row in result.rows
        if "interest_rate_pct" in row["fields"]
    ]
    assert len(rates) == len(_RATE_POLICIES)
    assert rates == sorted(rates)


def test_every_lineage_and_currency_negative_is_unrankable(monkeypatch, price_db):
    result, _captured = _ranked(monkeypatch, price_db)
    subjects = {row["subject"] for row in result.rows}
    figures = {row["fields"]["price_vnd"] for row in result.rows}

    for key in _NEGATIVES:
        assert normalize_subject_key(key) not in subjects, f"{key} must never take a price rank"
    assert "unit:soleil/ch03" not in subjects
    assert not figures & set(range(_NEGATIVE_BASE_VALUE, _NEGATIVE_BASE_VALUE + 100))
    assert 9_900_000_000 not in figures


def test_labelled_range_stays_available_on_a_non_ranked_spec(monkeypatch, price_db):
    """D-D: the contract constrains PRICE RANKS, not every facts query."""
    # No equality filter: match_semantics drops any range row under `=` (sql_leg:556),
    # so the availability claim is proved on an unfiltered spec.
    result, _captured = _run_on_db(
        monkeypatch,
        price_db,
        {"source": "facts", "limit": 50},
    )
    assert result.degraded is False
    labelled = [row for row in result.rows if row["subject"] == "unit:camellia/ch13a"]
    assert [row["quality"] for row in labelled] == ["range"]
    assert labelled[0]["trust_level"] == "estimate"
    assert labelled[0]["range"] == {
        "min": 1_980_000_000,
        "max": 2_640_000_000,
    }


def test_historical_as_of_cannot_claim_a_current_sale(monkeypatch, price_db):
    """An expired fact is history: asking for yesterday must not resurrect it."""
    spec = _ranked_price_spec()
    spec["filters"] = [
        {"field": "subject_key", "op": "=", "value": "unit:camellia/neg-fact-expired"}
    ]
    yesterday = price_db["today"] - timedelta(days=1)
    result, _captured = _run_on_db(monkeypatch, price_db, spec, as_of=yesterday)
    assert result.degraded is False
    assert result.rows == []


def test_half_open_interval_excludes_a_fact_ending_today(monkeypatch, price_db):
    spec = _ranked_price_spec()
    spec["filters"] = [
        {"field": "subject_key", "op": "=", "value": "unit:camellia/neg-fact-ends-today"}
    ]
    result, _captured = _run_on_db(monkeypatch, price_db, spec, as_of=price_db["today"])
    assert result.rows == []


# --- D. concrete exact-unit selection ---------------------------------------


def _exact_spec(query: str) -> dict:
    spec = _exact_unit_price_spec(query, PROJECT)
    assert spec is not None, query
    return validate_spec(spec)


def test_route_builds_the_canonical_subject_selector_for_an_exact_price_request(monkeypatch):
    ctx, calls = _drive_route(
        monkeypatch,
        "Giá chính thức căn CH-3 là bao nhiêu?",
        {"source": "facts", "filters": [], "limit": 10},
    )
    assert calls, "an exact confirmed price request must reach the SQL leg"
    spec = calls[0][0]
    assert spec["structured_path"] == "exact_unit_price"
    assert spec["filters"] == [
        {"field": "subject_key", "op": "=", "value": "unit:camellia/CH-03"},
        {"field": "price_vnd", "op": ">", "value": 0},
        {"field": "subject_type", "op": "=", "value": "unit"},
    ]
    assert ctx.store.raw["routed"].routing["needs_sql"] is True

def test_exact_unit_query_binds_the_persisted_normalized_subject():
    """E regression (no DB): canonical CH-03 persists as unit:camellia/ch03, so the
    exact-unit selectors must bind that persisted key while the spec keeps canonical form."""
    persisted = normalize_subject_key("unit:camellia/CH-03")
    assert persisted == "unit:camellia/ch03"
    spec = _exact_spec("Giá chính thức căn CH-03?")
    assert spec["filters"][0] == {
        "field": "subject_key",
        "op": "=",
        "value": "unit:camellia/CH-03",
    }
    assert sql_leg._unit_subject_keys(PROJECT, ["CH-03"]) == [persisted]
    _sql, params = build_sql(spec, date(2026, 8, 1), project_key=PROJECT)
    assert persisted in params
    assert workflow_module._unit_subject_code(persisted, PROJECT) == "CH-03"


def test_exact_unit_route_answers_only_its_own_canonical_unit(monkeypatch, price_db):
    """CH-3 resolves to CH-03; CH-11 holds the SAME figure and still cannot answer it."""
    result, _captured = _run_on_db(monkeypatch, price_db, _exact_spec("Giá chính thức căn CH-3?"))
    assert result.degraded is False
    assert [row["subject"] for row in result.rows] == ["unit:camellia/ch03"]
    assert result.rows[0]["fields"]["price_vnd"] == CONFIRMED_PRICE

    other, _captured = _run_on_db(
        monkeypatch, price_db, _exact_spec("Giá chính thức căn CH-11?")
    )
    assert [row["subject"] for row in other.rows] == ["unit:camellia/ch11"]
    assert other.rows[0]["fields"]["price_vnd"] == SAME_AMOUNT_OTHER_UNIT


def test_exact_unit_route_never_quotes_an_estimate(monkeypatch, price_db):
    """An estimate-only unit has no confirmed price to quote."""
    result, _captured = _run_on_db(
        monkeypatch, price_db, _exact_spec("Giá chính thức căn CH-13A?")
    )
    assert result.degraded is False
    assert result.rows == []


def test_exact_unit_path_requires_one_canonical_same_project_selector():
    base = {"source": "facts", "structured_path": "exact_unit_price", "limit": 1}
    for subject_value in (
        "unit:camellia/CH-3",  # the typed form, not the canonical stored key
        "unit:camellia/2pn-band",  # a type band is not one unit
        "unit:soleil/CH-03",  # another project's unit
    ):
        with pytest.raises(SpecError):
            build_sql(
                {
                    **base,
                    "filters": [{"field": "subject_key", "op": "=", "value": subject_value}],
                },
                date(2026, 8, 1),
                project_key=PROJECT,
            )
    # No subject selector at all is equally unanswerable.
    with pytest.raises(SpecError):
        build_sql({**base, "filters": []}, date(2026, 8, 1), project_key=PROJECT)


@pytest.mark.parametrize(
    "spec",
    [
        {"source": "facts", "order_by": {"field": "price_vnd", "dir": "asc"}, "limit": 10},
        {
            "source": "facts",
            "structured_path": "exact_unit_price",
            "filters": [
                {"field": "subject_key", "op": "=", "value": "unit:camellia/CH-3"},
            ],
            "order_by": {"field": "price_vnd", "dir": "asc"},
            "limit": 1,
        },
    ],
)
def test_builder_spec_defects_degrade_typed_without_touching_a_pool(spec, monkeypatch):
    """A builder raise is a spec defect: typed spec_invalid, no traceback, no pool."""
    touched: list[str] = []

    async def exploding_pool():
        touched.append("pool")
        raise AssertionError("an unbuildable spec must not reach the pool")

    def explode(_message, *_args, **_kwargs):
        touched.append("traceback")
        raise AssertionError("a spec defect must not log an unexpected-error traceback")

    monkeypatch.setattr(sql_leg, "get_ro_pool", exploding_pool)
    monkeypatch.setattr(sql_leg.logger, "exception", explode)
    result = asyncio.run(run_sql_leg(spec, None, "giá bao nhiêu", None))

    assert result.degraded is True
    assert result.meta["mode"] == "spec"
    assert result.meta["degraded_reason"] == "spec_invalid"
    assert result.meta["error"].startswith("spec invalid: ")
    assert touched == []


def test_generic_unit_price_question_stays_a_scoped_facts_spec(monkeypatch):
    """D-A: a plain 'Giá căn CH-03?' stays P0-03C's labelled-estimate path."""
    _ctx, calls = _drive_route(
        monkeypatch,
        "Giá căn CH-03?",
        {
            "source": "facts",
            "subject_type": "unit",
            "filters": [{"field": "subject_key", "op": "=", "value": "unit:camellia/CH-03"}],
            "limit": 5,
        },
    )
    assert calls, "a generic unit price question still runs the scoped facts spec"
    spec = calls[0][0]
    assert "structured_path" not in spec
    assert {"field": "subject_type", "op": "=", "value": "unit"} in spec["filters"]


def test_multi_unit_compare_is_not_one_units_exact_price():
    assert _exact_unit_price_spec("Giá chính thức căn CH-03 và CH-11?", PROJECT) is None


# --- E. rewrite prompt boundary ---------------------------------------------


class _CapturingLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list[dict] = []

    async def complete(self, messages, **_kwargs) -> str:
        self.messages = list(messages)
        return json.dumps(self.payload)


def _model_payload(sql_spec) -> dict:
    return {
        "rewritten_query": "Giá chính thức căn CH-03 của Camellia",
        "routing": {"needs_rag": True, "needs_sql": True, "structured_path": "spec"},
        "sql_spec": sql_spec,
        "hl_keywords": ["CH-03"],
        "ll_keywords": ["giá"],
        "high_stakes": False,
        "as_of": None,
    }


def test_rewrite_prompt_carries_no_server_identity_and_keeps_the_spec_shape(monkeypatch):
    """The model sees the closed field list only — no catalogue, no selected key."""
    llm = _CapturingLLM(
        _model_payload(
            {
                "source": "facts",
                "subject_type": "unit",
                "filters": [
                    {"field": "subject_key", "op": "=", "value": "unit:camellia/CH-03"}
                ],
                "order_by": {"field": "price_vnd", "dir": "asc"},
                "limit": 10,
            }
        )
    )
    monkeypatch.setattr(rewrite_module, "llm", llm)
    monkeypatch.setattr(rewrite_module, "model_for_role", lambda role: "fake-model")

    result = asyncio.run(
        rewrite_module.rewrite_query("Giá chính thức căn CH-03 của Camellia", [], None)
    )

    prompt = "\n".join(message["content"] for message in llm.messages)
    assert "The Camellia" not in prompt
    assert "soleil" not in prompt.lower()
    assert "active_catalogue" not in prompt
    # The facts/price shape survives the round trip untouched.
    assert result.sql_spec["subject_type"] == "unit"
    assert result.sql_spec["order_by"] == {"field": "price_vnd", "dir": "asc"}


def test_a_disobedient_model_still_cannot_widen_scope(monkeypatch):
    """Prompt guidance is not the control: route rejects what the validator refuses."""
    for value, expect_sql in (("%Camellia%", False), ("camellia", True)):
        llm = _CapturingLLM(
            _model_payload(
                {
                    "source": "facts",
                    "filters": [
                        {"field": "project_name", "op": "=", "value": value},
                        {"field": "area_m2", "op": ">", "value": 40},
                    ],
                    "limit": 10,
                }
            )
        )
        monkeypatch.setattr(rewrite_module, "llm", llm)
        monkeypatch.setattr(rewrite_module, "model_for_role", lambda role: "fake-model")
        routed = asyncio.run(rewrite_module.rewrite_query("diện tích lớn", [], None))

        _ctx, calls = _drive_route(monkeypatch, "diện tích lớn", routed.sql_spec)
        assert bool(calls) is expect_sql, value
