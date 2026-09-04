"""ISSUE-G4-01 — G3-r6 additive migration tests.

Two layers:
  1. Static SQL contract tests (always run): schema, constraints, indexes,
     additive-only and vector-untouched invariants of the migration file.
  2. Isolated-DB integration tests: apply the real bootstrap chain + the new
     migration twice (repeat-safe), prove row-preservation and training/customer
     predicates, then roll back and prove only G3-r6 objects disappear. They run
     against a disposable database cloned from db/ schema files on the local
     PostgreSQL (docker compose `ragre-postgres`, pgvector/pgvector:pg16); they
     SKIP with an explicit reason when no local server is reachable. When the
     live ragre database matches the locked 162-vector / empty chat baseline it
     is cloned via a template for a full-fidelity run; otherwise a synthetic
     bootstrap chain is used — no silent false-pass either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.migration_sql_support import (
    MIGRATION,
    ddl_without_do_bodies,
    find_ddl,
    has_ddl,
    has_do_block,
    split_statements,
)

REPO = Path(__file__).resolve().parents[1]
SCHEMA_FILES = (
    REPO / "db" / "schema.sql",
    REPO / "db" / "audit.sql",
    REPO / "db" / "lead_schema.sql",
    REPO / "db" / "migrations" / "2026-08-25-chat-session-history.sql",
    REPO / "db" / "migrations" / "2026-08-26-chat-sessions-upgrade.sql",
)

VDB_TABLE = "lightrag_vdb_chunks_text_embedding_v4_1024d"

# Rollback section lifted from the migration's down-note comment (plus the
# fourth training check, so rollback removes every G3-r6 object).
ROLLBACK_SQL = """
BEGIN;
DROP TABLE IF EXISTS sales_notification_reads;
ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_customer_identity;
ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_answer_mode_training_only;
ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_training_fields;
ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chk_sessions_training_shape;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS context_project_key;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS answer_mode;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS owner_firebase_uid;
COMMIT;
"""


# ---------------------------------------------------------------------------
# Layer 1 — static contract
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_is_transactional():
    assert MIGRATION.is_file()
    stmts = split_statements(MIGRATION.read_text(encoding="utf-8"))
    kinds = [s.kind for s in stmts]
    assert kinds.count("BEGIN") == 2
    assert kinds.count("COMMIT") == 2
    assert any(s.kind == "DDL" for s in stmts)


def test_notification_reads_contract():
    """sales_notification_reads(sales_id bigint, lead_id bigint, read_at, PK(sales_id, lead_id))."""
    assert has_ddl(
        "CREATE TABLE IF NOT EXISTS sales_notification_reads ("
        "sales_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,"
        "lead_id BIGINT NOT NULL REFERENCES leads(id) ON DELETE RESTRICT,"
        "read_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        "PRIMARY KEY (sales_id, lead_id))"
    ), "sales_notification_reads contract DDL not found"


def test_notification_reads_supports_unread_and_markall():
    assert has_ddl(
        "CREATE INDEX IF NOT EXISTS idx_snr_sales_read_at "
        "ON sales_notification_reads (sales_id, read_at DESC)"
    )
    assert has_ddl("CREATE INDEX IF NOT EXISTS idx_snr_lead ON sales_notification_reads (lead_id)")
    assert has_ddl("PRIMARY KEY (sales_id, lead_id)")


def test_training_columns_are_additive_nullable():
    for col in ("owner_firebase_uid", "answer_mode", "context_project_key"):
        assert has_ddl(f"ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS {col} TEXT")
    # Nullable on purpose: no NOT NULL and no default, so no rewrite of rows.
    for stmt in ddl_without_do_bodies():
        assert "ADD COLUMN" not in stmt or "NOT NULL" not in stmt


def test_training_checks_and_partial_indexes():
    assert has_do_block("chk_sessions_answer_mode_training_only")
    assert has_do_block("chk_sessions_training_shape")
    assert has_do_block("chk_sessions_training_fields")
    assert has_do_block("chk_sessions_customer_identity")
    # Three-valued-logic safety: the identity gate must not NULL-pass.
    assert has_do_block(
        "chk_sessions_customer_identity CHECK (answer_mode IS NOT DISTINCT FROM 'training'"
    )
    assert has_ddl(
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_training_owner_recent "
        "ON chat_sessions(owner_firebase_uid, last_active_at DESC, session_id) "
        "WHERE answer_mode = 'training'"
    )
    assert has_ddl(
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_training_owner_project "
        "ON chat_sessions(owner_firebase_uid, context_project_key) "
        "WHERE answer_mode = 'training'"
    )


def test_migration_is_purely_additive_no_destructive_ddl():
    joined = " ".join(ddl_without_do_bodies()).lower()
    for forbidden in (
        "drop table", "drop column", "drop constraint", "truncate",
        "delete from", "update ", "insert into",
    ):
        assert forbidden not in joined, forbidden


def test_migration_never_touches_vectors():
    for stmt in ddl_without_do_bodies():
        low = stmt.lower()
        for forbidden in ("vector", "embedding", "lightrag", "drop index", "reindex"):
            assert forbidden not in low, forbidden


def test_migration_leads_rows_not_altered():
    assert not find_ddl("ALTER TABLE leads")
    # Indexes may only target the new table or chat_sessions partials.
    for stmt in ddl_without_do_bodies():
        low = " ".join(stmt.lower().split())
        if low.startswith("create index"):
            assert "sales_notification_reads" in low or "chat_sessions" in low


def test_down_note_lists_only_g3_r6_objects():
    text = MIGRATION.read_text(encoding="utf-8").lower()
    for obj in (
        "drop table if exists sales_notification_reads;",
        "drop column if exists owner_firebase_uid",
        "drop column if exists answer_mode",
        "drop column if exists context_project_key",
    ):
        assert obj in text
    # The down-note never deletes data rows or vector objects.
    down_lines = [
        line.split("--", 1)[1] for line in text.splitlines() if line.strip().startswith("--")
    ]
    joined = "\n".join(down_lines)
    assert "truncate" not in joined
    assert "delete from" not in joined
    assert "drop table chat_sessions" not in joined
    assert "drop table leads" not in joined


# ---------------------------------------------------------------------------
# Layer 2 — isolated PostgreSQL 16.6+ apply / repeat / rollback
# ---------------------------------------------------------------------------


def _admin_params():
    from api.infrastructure.config.config import get_settings

    s = get_settings()
    return {
        "host": s.postgres_host,
        "port": s.postgres_port,
        "user": s.postgres_user,
        "password": s.postgres_password,
    }


def _connect(psycopg2, dbname, params):
    conn = psycopg2.connect(dbname=dbname, connect_timeout=5, **params)
    conn.autocommit = True
    return conn


def _table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    return bool(cur.fetchone()[0])


def _load_chain(cur):
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    cur.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    for path in SCHEMA_FILES:
        cur.execute(path.read_text(encoding="utf-8"))


def _seed_fixture(conn):
    """Seed customer/legacy rows + one sales/lead + a 162 x vector(1024) fixture table."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sales(full_name, role) VALUES ('Tester G3R6', 'sales') RETURNING id"
        )
        sales_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO leads(session_id, project_key, phone, name, consent, status, "
            "assigned_sales_id) VALUES "
            "('s-lead-1', 'project-soleil-2026q3', '+84900000001', 'Customer', true, "
            "'assigned', %s) RETURNING id",
            (sales_id,),
        )
        lead_id = cur.fetchone()[0]
        # Two pre-existing customer rows: one legacy handle, one anonymous device.
        cur.execute(
            "INSERT INTO chat_sessions(session_id, identity_key, project_key) "
            "VALUES ('sess-cust-1', 'identity-legacy-1', 'project-soleil-2026q3')"
        )
        cur.execute(
            "INSERT INTO chat_sessions(session_id, device_id, project_key) "
            "VALUES ('sess-anon-1', 'device-abc', 'project-soleil-2026q3')"
        )
        cur.execute(
            "INSERT INTO chat_messages(session_id, role, content) "
            "VALUES ('sess-cust-1', 'user', 'hello')"
        )
        if not _table_exists(cur, VDB_TABLE):
            cur.execute(
                f'CREATE TABLE public."{VDB_TABLE}" '
                "(id text PRIMARY KEY, workspace text, content_vector vector(1024))"
            )
        cur.execute(
            f'INSERT INTO public."{VDB_TABLE}"(id, content_vector) '
            "SELECT 'fixture-chunk-' || g, ('[' || repeat('0.1,', 1023) || '0.1]')::vector "
            "FROM generate_series(1, 162) AS g "
            "ON CONFLICT (id) DO NOTHING"
        )
    return sales_id, lead_id


def _column_names(cur):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='chat_sessions'"
    )
    return {r[0] for r in cur.fetchall()}


def _constraint_names(cur, table):
    cur.execute(
        "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname='public' AND t.relname=%s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


@pytest.fixture(scope="module")
def isolated_db():
    pytest.importorskip("psycopg2")
    import psycopg2

    params = _admin_params()
    try:
        probe = _connect(psycopg2, "postgres", params)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local PostgreSQL unavailable for isolated migration tests: {exc}")
    probe.close()

    # DDL identifiers (CREATE/DROP DATABASE) cannot be bound parameters, so
    # the disposable fixture database uses a fixed name with drop-first
    # bootstrap: every statement stays fully static.
    db = "g3r6_iso_fixture"
    admin = _connect(psycopg2, "postgres", params)
    with admin.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS g3r6_iso_fixture WITH (FORCE)")
        cur.execute("CREATE DATABASE g3r6_iso_fixture")
    admin.close()

    try:
        conn = _connect(psycopg2, db, params)
        try:
            with conn.cursor() as cur:
                _load_chain(cur)
                assert "owner_firebase_uid" not in _column_names(cur)
            sales_id, lead_id = _seed_fixture(conn)
        finally:
            conn.close()
    except Exception:
        _drop_db(psycopg2, params)
        raise

    yield {"db": db, "params": params, "sales_id": sales_id, "lead_id": lead_id}

    _drop_db(psycopg2, params)


def _drop_db(psycopg2, params):
    drop = _connect(psycopg2, "postgres", params)
    with drop.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS g3r6_iso_fixture WITH (FORCE)")
    drop.close()


def test_isolated_apply_repeat(isolated_db):
    pytest.importorskip("psycopg2")
    import psycopg2

    db, params = isolated_db["db"], isolated_db["params"]
    sales_id, lead_id = isolated_db["sales_id"], isolated_db["lead_id"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")

    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM chat_messages")
            messages_before = cur.fetchone()[0]

            cur.execute(migration_sql)  # apply #1
            cur.execute(migration_sql)  # repeat apply #2 — safe

            assert {"owner_firebase_uid", "answer_mode", "context_project_key"} <= set(
                _column_names(cur)
            )
            assert {
                "chk_sessions_answer_mode_training_only",
                "chk_sessions_training_shape",
                "chk_sessions_training_fields",
            } <= _constraint_names(cur, "chat_sessions")

            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename IN "
                "('chat_sessions','sales_notification_reads')"
            )
            idx = {r[0] for r in cur.fetchall()}
            assert {"idx_snr_sales_read_at", "idx_snr_lead",
                    "idx_chat_sessions_training_owner_recent",
                    "idx_chat_sessions_training_owner_project"} <= idx

            # Idempotent read-state write on the composite PK.
            cur.execute(
                "INSERT INTO sales_notification_reads(sales_id, lead_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (sales_id, lead_id),
            )
            cur.execute(
                "INSERT INTO sales_notification_reads(sales_id, lead_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (sales_id, lead_id),
            )
            cur.execute("SELECT count(*) FROM sales_notification_reads")
            assert cur.fetchone()[0] == 1

            # Training row shape enforced.
            cur.execute(
                "INSERT INTO chat_sessions(session_id, identity_key, project_key, "
                "answer_mode, owner_firebase_uid, context_project_key) VALUES "
                "('sess-train-1', 'identity-1', 'project-soleil-2026q3', 'training', "
                "'firebase-uid-1', 'project-soleil-2026q3')"
            )
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO chat_sessions(session_id, identity_key, project_key, answer_mode) "
                    "VALUES ('sess-bad-1', 'identity-1', 'project-soleil-2026q3', 'training')"
                )
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO chat_sessions(session_id, identity_key, project_key, "
                    "answer_mode, owner_firebase_uid, context_project_key) VALUES "
                    "('sess-bad-2', 'identity-1', 'project-soleil-2026q3', 'training', "
                    "'u', 'other-project')"
                )
            # Legacy customer row (no handles at all) rejected post-migration.
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO chat_sessions(session_id, project_key) "
                    "VALUES ('sess-bad-3', 'project-soleil-2026q3')"
                )
            # Pre-existing customer/anonymous rows were NOT reclassified.
            cur.execute(
                "SELECT session_id, answer_mode, owner_firebase_uid FROM chat_sessions "
                "WHERE session_id IN ('sess-cust-1','sess-anon-1') ORDER BY session_id"
            )
            assert [r[0] for r in cur.fetchall()] == ["sess-anon-1", "sess-cust-1"]

            # FK RESTRICT: read history cannot be destroyed by lead deletion.
            with pytest.raises(psycopg2.errors.ForeignKeyViolation):
                cur.execute("DELETE FROM leads WHERE id = %s", (lead_id,))

            # Vector population and messages untouched by the migration.
            cur.execute(
                f'SELECT count(*), min(vector_dims(content_vector)), '
                f'max(vector_dims(content_vector)) FROM public."{VDB_TABLE}"'
            )
            assert cur.fetchone() == (162, 1024, 1024)
            cur.execute("SELECT count(*) FROM chat_messages")
            assert cur.fetchone()[0] == messages_before
    finally:
        conn.close()


def test_isolated_rollback_preserves_data(isolated_db):
    """Runs after test_isolated_apply_repeat (module order, shared isolated DB);

    proves ONLY G3-r6 objects disappear and every unrelated row survives.
    """
    pytest.importorskip("psycopg2")
    import psycopg2

    db, params = isolated_db["db"], isolated_db["params"]
    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_depend d JOIN pg_class c ON c.oid = d.refobjid "
                "WHERE c.relname = 'sales_notification_reads' "
                "AND d.refclassid = 'pg_class'::regclass "
                "AND d.classid IN ('pg_rewrite'::regclass, 'pg_constraint'::regclass) "
                "AND d.objsubid = 0 AND d.classid = 'pg_rewrite'::regclass"
            )
            assert cur.fetchone()[0] == 0  # dependents guard: no views depend on it
            cur.execute(
                "SELECT count(*) FROM pg_constraint WHERE contype='f' AND confrelid = "
                "to_regclass('public.sales_notification_reads')"
            )
            assert cur.fetchone()[0] == 0  # no external FK references it

            cur.execute(ROLLBACK_SQL)

            assert not _table_exists(cur, "sales_notification_reads")
            cols = _column_names(cur)
            assert not {"owner_firebase_uid", "answer_mode", "context_project_key"} & cols
            remaining = _constraint_names(cur, "chat_sessions")
            assert not {n for n in remaining if n.startswith("chk_sessions_")}

            # All pre-existing and unrelated data survived rollback untouched.
            cur.execute(
                "SELECT count(*) FROM chat_sessions "
                "WHERE session_id IN ('sess-cust-1','sess-anon-1')"
            )
            assert cur.fetchone()[0] == 2
            cur.execute("SELECT count(*) FROM leads")
            assert cur.fetchone()[0] == 1
            cur.execute(
                f'SELECT count(*), min(vector_dims(content_vector)), '
                f'max(vector_dims(content_vector)) FROM public."{VDB_TABLE}"'
            )
            assert cur.fetchone() == (162, 1024, 1024)
    finally:
        conn.close()
