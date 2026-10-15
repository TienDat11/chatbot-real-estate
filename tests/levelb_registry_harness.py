"""Level B proof harness — the PRODUCTION-SHAPED chain over a disposable registry.

``tests/test_final_answer_integrity.py`` proves token == answer == audit hash
with a HAND-BUILT pool: ``Connection.fetch`` returns a literal record list and
``write_audit`` appends to a Python list. Publication authority is therefore
decided against fixtures the test wrote, not against a database.

This module replaces BOTH fakes with the real path:

    LightRAG seam (only fake)
        -> real ``rag_leg.run_rag_leg`` / ``_post_filter`` (registry JOIN)
        -> real ``merge.merge_context`` / ``hydrate_chunks`` (second JOIN)
        -> real ``generate.build_messages`` (RAG_CONTEXT serialization)
        -> real ``guard_output`` + the real publication gate in the workflow
        -> real ``audit.write_audit`` -> ``query_audit`` row

Only the LLM stream, the input guard, the rewriter and the SQL leg stay faked;
the SQL leg returns ZERO facts, so every claim is descriptive and every byte of
publication authority has to come from the seeded registry rows.

The registry is a real, disposable LOCAL PostgreSQL database named
``levelb_registry``, created from ``db/schema.sql`` + ``db/audit.sql``. It is
reached through the production DSN builder (``sql_leg.build_dsn`` ->
``api.get_cfg`` -> cached ``Settings``), which is why the fixture re-points
``POSTGRES_*`` and clears the settings cache + every cached pool BEFORE the
first connection: ``merge.py`` binds ``get_ro_pool`` at import time, so
patching the symbol would NOT reach the hydration read.

    LEVELB_TEST_PG_ADMIN=postgresql://ragre:pw@127.0.0.1:5432/postgres \\
        python -m pytest -q tests/test_levelb_workflow_proof.py
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

import api.application.pipelines.workflow as workflow_module
import api.application.services.rag_leg as rag_leg_module
import api.application.services.sql_leg as sql_leg
from api.application.pipelines.conv_workflow import RagQueryPipelineConv
from api.application.pipelines.workflow import RagQueryPipeline
from api.application.services.generate import build_messages
from api.application.services.project_context import ProjectContext
from api.application.services.sql_leg import ConfirmedPriceProbe, SqlLegResult
from api.domain.services.guard_input import GuardResult as InputGuardResult
from api.domain.services.guard_output import guard_output
from api.domain.services.rewrite import RoutedResult
from api.domain.value_objects.constants import (
    SSE_EVENT_FACTS,
    SSE_EVENT_IMAGES,
    SSE_EVENT_PLACES,
    SSE_EVENT_SOURCES,
    SSE_EVENT_TOKEN,
)
from api.infrastructure.config.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "db" / "schema.sql"
AUDIT_SQL = REPO_ROOT / "db" / "audit.sql"

DBNAME = "levelb_registry"
GATED_EVENTS = (
    SSE_EVENT_TOKEN, SSE_EVENT_SOURCES, SSE_EVENT_FACTS, SSE_EVENT_IMAGES, SSE_EVENT_PLACES,
)


def sha256_hex(text: str) -> str:
    """Independent oracle — NOT the production ``sha256_hex``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Disposable database
# ---------------------------------------------------------------------------


def admin_dsn() -> str:
    """The opt-in admin DSN. Unset -> skip; shared host -> hard fail."""
    dsn = os.environ.get("LEVELB_TEST_PG_ADMIN", "").strip()
    if not dsn:
        pytest.skip(
            "LEVELB_TEST_PG_ADMIN is unset — the Level B gate needs a disposable "
            "local PostgreSQL (never a shared/production target)"
        )
    host = (urlparse(dsn).hostname or "").lower()
    if "pooler" in host or "supabase" in host or "rds" in host:
        pytest.fail(f"refusing a shared/production PostgreSQL target: {host}")
    return dsn


def dsn_for(admin: str, dbname: str = DBNAME) -> str:
    return urlparse(admin)._replace(path=f"/{dbname}").geturl()


def _drop_disposable(psycopg2, admin: str, dbname: str = DBNAME) -> None:
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        conn.close()


async def _close_pools_in_loop() -> None:
    """Close every cached pool from within the loop that owns it.

    ``sql_leg._ro_pool`` has NO loop guard (unlike ``audit._audit_pool``), and
    every ``asyncio.run`` opens a new loop, so a pool cached across turns is
    bound to a dead loop. Closing it from the NEXT loop raises and abandons the
    live connections, which leaks the pooler's session slots; awaiting the
    close on the SAME loop releases them cleanly. The registry adapter and the
    project_config TTL cache are dropped for the same loop reason.
    """
    import api.application.services.audit as audit_module
    import api.application.services.sql_leg as sql_leg_module

    try:
        await sql_leg_module.close_ro_pool()
    except Exception:  # noqa: BLE001 — teardown must never fail a run
        sql_leg_module._ro_pool = None
    try:
        await audit_module.close_audit_pool()
    except Exception:  # noqa: BLE001
        audit_module._audit_pool = None
        audit_module._audit_pool_loop = None
    try:
        from api.infrastructure.dependencies import get_project_registry

        registry = get_project_registry()
        close = getattr(registry, "close", None)
        if close is not None:
            await close()
    except Exception:  # noqa: BLE001
        pass
    from api.application.services import project_config as project_config_module

    project_config_module._PROJECT_CACHE.clear()


def close_cached_pools() -> None:
    """Sync wrapper for call sites with no running loop (module teardown)."""
    try:
        asyncio.run(_close_pools_in_loop())
    except Exception:  # noqa: BLE001 — teardown must never fail a run
        pass


_ENV_KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE")
_ENV_SNAPSHOT: dict[str, str | None] | None = None


def _wire_env_to_disposable(db_dsn: str) -> None:
    """Point the PRODUCTION DSN builder at the disposable database."""
    global _ENV_SNAPSHOT
    # Snapshot once so module teardown can restore the process env: other test
    # modules build their own DSNs from POSTGRES_* after this fixture drops its
    # database, and a leaked ``POSTGRES_DATABASE=levelb_registry`` would make
    # every later pool open against a database that no longer exists.
    if _ENV_SNAPSHOT is None:
        _ENV_SNAPSHOT = {key: os.environ.get(key) for key in _ENV_KEYS}
    parts = urlparse(db_dsn)
    os.environ["POSTGRES_HOST"] = parts.hostname or "127.0.0.1"
    os.environ["POSTGRES_PORT"] = str(parts.port or 5432)
    os.environ["POSTGRES_USER"] = parts.username or "ragre"
    os.environ["POSTGRES_PASSWORD"] = unquote(parts.password or "")
    os.environ["POSTGRES_DATABASE"] = parts.path.lstrip("/") or DBNAME
    # ``get_settings`` is @lru_cache(maxsize=1): without this the first import
    # in the session froze the real corpus DSN and every pool below would open
    # against production.
    get_settings.cache_clear()
    close_cached_pools()
    try:
        built = sql_leg.build_dsn()
    except NameError as exc:
        # Fail closed, loudly, and name the production defect instead of
        # letting a swallowed NameError look like "the registry was empty".
        pytest.fail(
            f"sql_leg.build_dsn() raised {exc!r}: api/application/services/sql_leg.py "
            "calls urllib.parse.quote() but imports no urllib. Every pool built from "
            "this DSN (get_ro_pool / get_audit_pool) fails, so registry hydration "
            "and the query_audit INSERT can never succeed."
        )
    assert DBNAME in built, f"production DSN builder did not follow the fixture: {built}"
    assert parts.hostname in built, f"production DSN builder kept another host: {built}"


def _restore_env() -> None:
    """Undo ``_wire_env_to_disposable`` so later modules see the real env."""
    global _ENV_SNAPSHOT
    if _ENV_SNAPSHOT is None:
        return
    for key, value in _ENV_SNAPSHOT.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _ENV_SNAPSHOT = None
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def levelb_registry():
    """Disposable registry cloned from db/schema.sql + db/audit.sql."""
    pytest.importorskip("psycopg2")
    import psycopg2

    admin = admin_dsn()
    db_dsn = dsn_for(admin)

    _drop_disposable(psycopg2, admin)
    admin_conn = psycopg2.connect(admin)
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{DBNAME}"')
    admin_conn.close()

    conn = psycopg2.connect(db_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for extension in ("vector", "btree_gist", "pgcrypto"):
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            cur.execute(AUDIT_SQL.read_text(encoding="utf-8"))
            cur.execute("GRANT ro_query TO ragre")
            cur.execute("GRANT INSERT, SELECT ON query_audit TO ragre")
            cur.execute(
                "SELECT current_user, rolsuper, "
                "(SELECT rolname FROM pg_roles WHERE rolname = 'ro_query') "
                "FROM pg_roles WHERE rolname = current_user"
            )
            current_user, is_super, ro_query_exists = cur.fetchone()
            # Empirically confirm the read path the app actually uses: the owner
            # connects with NO ``SET LOCAL ROLE``, so the FORCE RLS policies
            # apply unless the owner is a superuser — then the seeded rows are
            # invisible and every hydration read silently degrades to [].
            assert is_super is True, (
                f"{current_user} must be a superuser for the owner-connect read path"
            )
            assert ro_query_exists == "ro_query", (
                "db/schema.sql must have created the ro_query role (NOLOGIN by "
                "design); without it SET LOCAL ROLE fails inside with_rls_identity"
            )
            cur.execute("SELECT CURRENT_DATE")
            today = cur.fetchone()[0]
            cur.execute("SELECT has_table_privilege('ragre', 'query_audit', 'INSERT')")
            assert cur.fetchone()[0] is True, "real write_audit needs INSERT on query_audit"
            # The registry hub, read by the REAL load_project_registry_record.
            seed_project_config(cur, project_key="camellia", ten_thuong_mai="Camellia")
            seed_project_config(cur, project_key="soleil", ten_thuong_mai="Soleil")
        conn.close()
    except BaseException:
        try:
            conn.close()
        finally:
            _drop_disposable(psycopg2, admin)
        raise

    _wire_env_to_disposable(db_dsn)
    yield {"dsn": db_dsn, "today": today, "psycopg2": psycopg2}
    close_cached_pools()
    _restore_env()
    _drop_disposable(psycopg2, admin)


@pytest.fixture(autouse=True)
def _levelb_pool_isolation():
    """No cached asyncpg pool may survive a turn (loop-bound, see above)."""
    yield
    close_cached_pools()


def seed_registry(cur, *, doc_id, chunk_id, content, project_key, title,
                  effective_from, effective_to=None, status="published",
                  kind="project", section="Tiện ích", chunk_index=0):
    """One document + its chunk, exactly the columns the registry reads.

    Inserted as the connecting ``ragre`` role, i.e. through the same write
    policy the ingest side uses.
    """
    cur.execute(
        """
        INSERT INTO documents
            (doc_id, kind, title, source_file, effective_from, effective_to,
             status, content_hash, version, lightrag_doc_id, project_key, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, '{}'::jsonb)
        ON CONFLICT (doc_id) DO NOTHING
        """,
        (doc_id, kind, title, f"{doc_id}.pdf", effective_from, effective_to,
         status, sha256_hex(content), doc_id, project_key),
    )
    cur.execute(
        """
        INSERT INTO document_chunks (doc_id, chunk_id, chunk_index, content, text_hash, section)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (chunk_id) DO NOTHING
        """,
        (doc_id, chunk_id, chunk_index, content, sha256_hex(content), section),
    )


def seed_project_config(cur, *, project_key, ten_thuong_mai, vi_tri="Da Nang"):
    """The registry hub row the REAL ``load_project_registry_record`` reads."""
    cur.execute(
        """
        INSERT INTO project_config (project_key, ten_phap_ly, ten_thuong_mai, vi_tri,
                                    location, is_hot, geo_center_lat, geo_center_lng,
                                    hotline, media, status)
        VALUES (%s, %s, %s, %s, %s, false, NULL, NULL, NULL, '[]'::jsonb, 'active')
        ON CONFLICT (project_key) DO NOTHING
        """,
        (project_key, f"Công ty {ten_thuong_mai}", ten_thuong_mai, vi_tri, vi_tri),
    )


@contextmanager
def registry(db):
    """Autocommitting cursor on the disposable database, for seeding only."""
    conn = db["psycopg2"].connect(db["dsn"])
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def audit_row(psycopg2, db_dsn: str, trace_id: str) -> dict | None:
    """Read the REAL ``query_audit`` row written by the real ``write_audit``."""
    conn = psycopg2.connect(db_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT trace_id, query, chunk_ids, answer_hash, confidence, "
                "guard_verdicts, degraded, latency_ms "
                "FROM query_audit WHERE trace_id = %s",
                (trace_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "trace_id": row[0], "query": row[1], "chunk_ids": row[2] or [],
                "answer_hash": row[3], "confidence": row[4],
                "guard_verdicts": row[5] or {}, "degraded": row[6] or {},
                "latency_ms": row[7],
            }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Reranker:
    async def rerank(self, query, chunks):
        return chunks


class _FakeLightRag:
    """The ONLY fake in the retrieval chain.

    ``aquery_data`` returns exactly what LightRAG 1.5.6 returns; chunk identity
    travels in ``file_path`` (what ``rag_leg._normalize_chunks`` reads) and the
    content is byte-identical to the seeded ``document_chunks.content``, which
    is what ``merge._eligible_chunk`` demands. Everything after this point is
    production code reading the disposable registry.
    """

    def __init__(self, chunks: dict[str, str]) -> None:
        self.chunks = dict(chunks)
        self.queries: list[str] = []

    async def aquery_data(self, query, param=None):
        self.queries.append(query)
        return {
            "data": {
                "chunks": [
                    {"file_path": cid, "content": content, "score": 0.9}
                    for cid, content in self.chunks.items()
                ]
            }
        }


class LevelBHarness:
    """Seam installation for one REAL registry-backed workflow run."""

    def __init__(self, monkeypatch, *, db, project, query, draft,
                 rag_chunks: dict[str, str], session_id: str,
                 guard_result=None) -> None:
        self.monkeypatch = monkeypatch
        self.db = db
        self.project = project
        self.query = query
        self.draft = draft
        self.session_id = session_id
        self.prompts: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self.order: list[str] = []
        self.fake_rag = _FakeLightRag(rag_chunks)
        self.rag_chunk_ids = tuple(rag_chunks)
        self._guard_result = guard_result

        async def fake_guard(raw):
            return InputGuardResult(clean=raw, rejected=False, degraded=True)

        async def fake_rewrite(clean, history, as_of_iso):
            return RoutedResult(
                rewritten=clean,
                routing={
                    "needs_rag": True, "needs_sql": False,
                    "structured_path": "none", "needs_geo": False,
                },
                sql_spec=None,
                hl_keywords=[], ll_keywords=[], high_stakes=False, as_of=None,
            )

        async def fake_sql(*args, **kwargs):
            # Zero SQL facts: every published claim must rest on RAG evidence.
            return SqlLegResult([], {"mode": "none"})

        async def fake_stream(merged, history, high_stakes):
            # The REAL prompt builder runs — _prompt_rag_rows / _prompt_fact_rows
            # are what publication authority is decided against.
            messages = build_messages(merged, history)
            self.prompts.append("\n".join(str(m.get("content", "")) for m in messages))
            yield self.draft

        async def observing_guard(answer, facts, sources, routing, meta=None):
            if self._guard_result is not None:
                return self._guard_result
            return await guard_output(answer, facts, sources, routing, meta=meta)

        async def price_probe(project_key, unit_codes=None, **kwargs):
            return ConfirmedPriceProbe(True, None, frozenset(), False)

        m = self.monkeypatch
        m.setattr(workflow_module, "guard_input", fake_guard)
        m.setattr(workflow_module, "rewrite_query", fake_rewrite)
        m.setattr(workflow_module, "get_reranker", lambda: _Reranker())
        m.setattr(workflow_module, "run_sql_leg", fake_sql)
        m.setattr(workflow_module, "stream_answer", fake_stream)
        m.setattr(workflow_module, "guard_output", observing_guard)
        m.setattr(workflow_module, "probe_confirmed_price", price_probe)
        m.setattr(
            workflow_module, "search_images", lambda *a, **k: asyncio.sleep(0, result=[])
        )
        m.setattr(
            workflow_module, "fetch_price_board_images",
            lambda *a, **k: asyncio.sleep(0, result=[]),
        )
        # NOT patched, deliberately: project_config.load_project_registry_record
        # (real registry-hub read), merge_module.get_ro_pool (real hydration
        # read) and workflow_module.write_audit (real query_audit INSERT).
        # LightRAG seam only. run_rag_leg / _post_filter stay the real functions.
        m.setattr(rag_leg_module, "_get_rag", lambda project_key=None: self.fake_rag)
        # NOT patched, deliberately: merge_module.get_ro_pool (real hydration
        # read) and workflow_module.write_audit (real query_audit INSERT).

    # -- run ---------------------------------------------------------------
    def _on_event(self, event: str, data: dict) -> None:
        self.events.append((event, data))
        self.order.append(f"emit:{event}")

    def pipeline(self, facade: str):
        return (
            RagQueryPipelineConv(on_event=self._on_event) if facade == "conv"
            else RagQueryPipeline(on_event=self._on_event)
        )

    def run(self, *, facade: str = "direct", visible_limit=None) -> dict:
        async def go():
            # Close every pool on the SAME loop that created it, so no live
            # connection is abandoned to a dead loop (a cross-loop close raises
            # and leaks the pooler's session slots). The next turn rebuilds.
            try:
                return await self.pipeline(facade).run(
                    self.query,
                    self.session_id,
                    None,
                    [],
                    project_context=self.project,
                    visible_limit=visible_limit,
                )
            finally:
                await _close_pools_in_loop()

        return asyncio.run(go())

    # -- observations ------------------------------------------------------
    def gated(self) -> list[str]:
        return [name for name, _ in self.events if name in GATED_EVENTS]

    def tokens(self) -> str:
        return "".join(d.get("text", "") for name, d in self.events if name == SSE_EVENT_TOKEN)

    def prompt(self) -> str:
        """The REAL serialized prompt the publication gate was decided against."""
        assert self.prompts, "build_messages never ran"
        return self.prompts[-1]

    def audit_row(self, trace_id: str) -> dict | None:
        return audit_row(self.db["psycopg2"], self.db["dsn"], trace_id)

    def assert_bytes_agree(self, result: dict, expected: str) -> dict:
        """The one invariant, checked against the REAL audit row.

        token bytes == returned answer == the ``answer_hash`` the real
        ``write_audit`` INSERTed into ``query_audit`` for this trace.
        """
        expected_bytes = expected.encode("utf-8")
        assert self.tokens().encode("utf-8") == expected_bytes
        assert result["answer"].encode("utf-8") == expected_bytes
        row = self.audit_row(result["trace_id"])
        assert row is not None, f"no real query_audit row for trace {result['trace_id']}"
        assert row["answer_hash"] == sha256_hex(expected)
        return row


def soleil_context() -> ProjectContext:
    return ProjectContext("soleil", "normal", (("camellia", "Camellia"), ("soleil", "Soleil")))


def camellia_context() -> ProjectContext:
    return ProjectContext("camellia", "normal", (("camellia", "Camellia"), ("soleil", "Soleil")))


def opened_window(today: date) -> tuple[date, None]:
    """An open-ended, already-effective validity window."""
    return (today - timedelta(days=30), None)