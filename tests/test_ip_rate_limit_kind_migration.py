"""Contract + executable tests for the ip_rate_limit kind widening migration.

Two layers:
  1. Static SQL contract tests (always run): transactional shape, READ
     COMMITTED isolation pinned as the first statement after BEGIN (before
     the advisory lock), advisory-lock ordering (lock taken BEFORE the
     pg_constraint read, held through DDL), fail-closed missing-constraint
     and drift handling, additive-only widening, semantic (skeleton + kind
     set) validation instead of substring matching.
  2. Isolated-DB executable tests: apply the real migration on a disposable
     database cloned from db/schema.sql, prove repeat idempotence, prove the
     second of two concurrent transactions BLOCKS on the advisory lock
     (deterministically, via lock_timeout cancellation) and then completes
     observing the widened definition, prove a missing constraint fails
     closed, prove a drifted constraint fails closed with rollback and no
     silent repair, prove the migration succeeds as the first statement of a
     REPEATABLE READ caller and fails closed when invoked mid-transaction,
     and prove two concurrent full-migration executions starting from fresh
     narrow state are BOTH first observed waiting on the advisory lock via
     server-side lock-state inspection (pg_stat_activity, pg_locks,
     pg_blocking_pids) while a dedicated holder owns the lock, and only
     then complete with the exact target constraint. They SKIP
     with an explicit reason when no local server is reachable. DB config
     comes from the environment via get_settings(); no credential literals,
     no printed values.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

try:
    import psycopg2
except ImportError:  # executable layer skips via importorskip
    psycopg2 = None  # type: ignore[assignment]

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "2026-09-03-ip-rate-limit-kind-fcm-register.sql"
)

REPO = Path(__file__).resolve().parents[1]

LOCK_STMT = "SELECT pg_advisory_xact_lock(5393038, 1);"
LOCK_TIMEOUT_CANCELED = "300ms"

# Exact canonical pg_get_constraintdef(oid, true) rendering on PostgreSQL 16
# for the widened four-kind membership check.
TARGET_DEF = (
    "CHECK (kind = ANY (ARRAY['query'::text, 'mint'::text, 'lead'::text, 'fcm-register'::text]))"
)
FOUR_KINDS = ("query", "mint", "lead", "fcm-register")

# Paren/cast-normalized shape-guard skeletons (verified against the actual
# PostgreSQL 16.14 renderings; see _canonical_skeleton regression test).
NARROW_SKELETON = "CHECKkind=ANYARRAY[?,?,?]"
TARGET_SKELETON = "CHECKkind=ANYARRAY[?,?,?,?]"


# ---------------------------------------------------------------------------
# Layer 1 — static contract
# ---------------------------------------------------------------------------


def _migration_code() -> str:
    """Migration SQL with comment lines stripped (prose cannot game checks)."""
    sql = MIGRATION.read_text(encoding="utf-8")
    return "\n".join(
        line for line in sql.lower().splitlines() if not line.lstrip().startswith("--")
    )


def test_ip_rate_limit_kind_migration_is_transactional_and_idempotent():
    code = _migration_code()
    # Single transaction: BEGIN (after the header comment) matched by COMMIT.
    assert code.count("begin;") == 1
    assert code.rstrip().endswith("commit;")
    # Re-runs must be no-ops: semantic comparison against the exact target
    # state (skeleton + kind set), not a substring probe of the definition.
    assert "not like" not in code
    assert "regexp_replace" in code
    assert "array_agg" in code
    assert "target_skeleton" in code and "target_kinds" in code
    assert "refusing unsafe migration" in code


def test_ip_rate_limit_kind_migration_pins_read_committed_first():
    """Isolation is pinned as the first statement after BEGIN, before the lock."""
    code = _migration_code()
    begin = code.index("begin;")
    iso = code.index("set transaction isolation level read committed")
    lock = code.index("pg_advisory_xact_lock(")
    assert begin < iso < lock
    # No session-level or LOCAL isolation override sneaking around the pin.
    assert "set session" not in code
    assert "default_transaction_isolation" not in code


def test_ip_rate_limit_kind_migration_locks_before_reading_constraint():
    """Advisory lock must precede the pg_constraint read and hold through DDL."""
    code = _migration_code()
    sql = MIGRATION.read_text(encoding="utf-8")
    # Transaction-scoped (not session-scoped) advisory lock.
    assert "pg_advisory_xact_lock(" in code
    assert "pg_advisory_lock(" not in code
    # Ordering inside the transaction:
    # BEGIN < isolation pin < lock < pg_constraint read < DDL.
    begin = code.index("begin;")
    iso = code.index("set transaction isolation level read committed")
    lock = code.index("pg_advisory_xact_lock(")
    read = code.index("from pg_constraint")
    ddl = code.index("drop constraint")
    assert begin < iso < lock < read < ddl
    # The lock is acquired outside the DO block, as its own statement, so the
    # DO block gets a fresh READ COMMITTED snapshot after any lock wait.
    do_block = code.index("do $$")
    assert begin < iso < lock < do_block
    # Fixed two-int constant key; no hashing or input-derived key material.
    assert "5393038, 1" in sql
    for banned in ("hashtext", "hashtextextended", "hash_extended_combine"):
        assert banned not in sql.lower()


def test_ip_rate_limit_kind_migration_fails_closed():
    """No broad exception swallowing; missing OR drifted constraint aborts."""
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    # The old duplicate_object handler masked real drift and is gone.
    assert "duplicate_object" not in sql
    assert "exception\n  when" not in sql and "when others" not in sql
    # Fail-closed raise covers both missing and drifted states.
    assert sql.count("refusing unsafe migration") >= 2
    assert "raise exception" in sql
    assert "drift" in sql


def test_ip_rate_limit_kind_migration_is_strict_widening():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for kind in ("query", "mint", "lead", "fcm-register"):
        assert kind in sql
    # Forward-only: no destructive statements anywhere.
    assert "drop table" not in sql
    assert "truncate" not in sql
    assert "drop index" not in sql
    assert "drop column" not in sql


def test_ip_rate_limit_kind_migration_semantic_validation_is_exact():
    """Validation compares skeleton + sorted kind set, order/format tolerant."""
    sql = MIGRATION.read_text(encoding="utf-8")
    # Canonical skeleton constants: quoted literals (with their ::text cast)
    # -> '?', residual ::text casts, ALL parentheses, and whitespace stripped;
    # exact membership-check shape for 3 (narrow) and 4 (target) values. The
    # paren strip also accepts the non-pretty CHECK ((kind = ANY (...)))
    # rendering and any balanced redundant wrapping.
    assert "'CHECKkind=ANYARRAY[?,?,?]'" in sql
    assert "'CHECKkind=ANYARRAY[?,?,?,?]'" in sql
    assert "translate(skeleton, '()', '')" in sql
    assert "replace(skeleton, '::text', '')" in sql
    # Sorted (order-independent) kind lists compared with array equality.
    assert "ARRAY['lead', 'mint', 'query']" in sql
    assert "ARRAY['fcm-register', 'lead', 'mint', 'query']" in sql
    # Literal extraction preserves duplicates (no DISTINCT collapse): the
    # non-distinct aggregate feeds a count-vs-distinct duplicate tripwire.
    code = sql.lower()
    assert "array_agg(m order by m)" in code
    assert "array_agg(distinct" not in code.split("kinds :=")[0]
    assert "array_length(all_kinds, 1) = coalesce(array_length(kinds, 1), 0)" in code
    # Unknown/malformed states reach the drift RAISE, never a silent repair.
    assert "observed: %" in sql


def test_ip_rate_limit_kind_migration_has_no_dynamic_sql_or_secrets():
    sql = MIGRATION.read_text(encoding="utf-8")
    # Static DDL only: no f-string/format interpolation markers, no
    # credential or external resource references.
    assert 'f"' not in sql and "{!" not in sql
    assert "http://" not in sql and "https://" not in sql
    assert "password" not in sql.lower()
    assert "api_key" not in sql.lower()


def test_schema_snapshot_matches_target_definition():
    """Fresh bootstraps (db/schema.sql) must equal the migration's target."""
    schema = (REPO / "db" / "schema.sql").read_text(encoding="utf-8")
    assert "CHECK (kind IN ('query', 'mint', 'lead', 'fcm-register'))" in schema


def _canonical_skeleton(definition: str) -> str:
    """Python mirror of the migration's shape-guard canonicalization.

    Mirrors, step for step, the DO block: quoted literals (with the ::text
    cast PostgreSQL renders for text values) collapse to '?', residual
    ::text casts drop, ALL parentheses strip, whitespace strips. Used to
    prove that both pg_get_constraintdef(oid, true) and the non-pretty
    double-wrapped rendering canonicalize to the same accepted skeleton,
    while shape mutations (extra conjunct, IN-list arity change) do not.
    """
    import re

    s = re.sub(r"'[^']*'::text", "?", definition)
    s = re.sub(r"'[^']*'", "?", s)
    s = s.replace("::text", "")
    s = s.translate(str.maketrans("", "", "()"))
    return re.sub(r"\s+", "", s)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        # Actual pg_get_constraintdef(oid, true) output on PG 16.14 for the
        # narrow pre-migration constraint (verified against the live server).
        (
            "CHECK (kind = ANY (ARRAY['query'::text, 'mint'::text, 'lead'::text]))",
            NARROW_SKELETON,
        ),
        # Actual pg_get_constraintdef(oid, true) output for the widened target.
        (
            "CHECK (kind = ANY (ARRAY['query'::text, 'mint'::text, "
            "'lead'::text, 'fcm-register'::text]))",
            TARGET_SKELETON,
        ),
        # Non-pretty pg_get_constraintdef(oid, false) rendering: extra outer
        # parentheses layer must NOT cause a false drift rejection.
        (
            "CHECK ((kind = ANY (ARRAY['query'::text, 'mint'::text, "
            "'lead'::text, 'fcm-register'::text])))",
            TARGET_SKELETON,
        ),
        # Balanced redundant wrapping variants are accepted formatting noise.
        (
            "CHECK (((kind = ANY (ARRAY['query'::text, 'mint'::text, 'lead'::text]))))",
            NARROW_SKELETON,
        ),
        # Shape guard still fails closed: extra conjunct breaks the skeleton
        # (parens stripped, but the AND-clause residue makes it non-equal).
        (
            "CHECK (kind = ANY (ARRAY['query'::text, 'mint'::text, "
            "'lead'::text, 'fcm-register'::text])) AND (kind IS NOT NULL)",
            "CHECKkind=ANYARRAY[?,?,?,?]ANDkindISNOTNULL",
        ),
    ],
)
def test_skeleton_canonicalization_accepts_formatting_variants(definition, expected):
    """Regression: paren/cast rendering variants canonicalize identically;
    genuine shape mutations still fail the shape guard."""
    assert _canonical_skeleton(definition) == expected
    # Guard arity: narrow skeleton must not match a 4-slot shape and vice versa.
    assert NARROW_SKELETON != TARGET_SKELETON


# ---------------------------------------------------------------------------
# Layer 2 — isolated PostgreSQL apply / repeat / concurrency / fail-closed /
# isolation
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


def _connect(psycopg2, dbname, params, autocommit=True):
    conn = psycopg2.connect(dbname=dbname, connect_timeout=5, **params)
    conn.autocommit = autocommit
    return conn


def _constraint_def(cur):
    cur.execute(
        "SELECT pg_get_constraintdef(oid, true) FROM pg_constraint "
        "WHERE conname = 'ip_rate_limit_kind_check' "
        "AND conrelid = 'public.ip_rate_limit'::regclass"
    )
    row = cur.fetchone()
    return row[0] if row else None


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
    db = "iprl_iso_fixture"
    admin = _connect(psycopg2, "postgres", params)
    with admin.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS iprl_iso_fixture WITH (FORCE)")
        cur.execute("CREATE DATABASE iprl_iso_fixture")
    admin.close()

    try:
        conn = _connect(psycopg2, db, params)
        try:
            with conn.cursor() as cur:
                # Fresh bootstrap from the current schema snapshot: the inline
                # CHECK is auto-named ip_rate_limit_kind_check and is already
                # the widened target — exactly the state the migration must
                # treat as an idempotent no-op.
                cur.execute((REPO / "db" / "schema.sql").read_text(encoding="utf-8"))
                seed = (
                    "INSERT INTO ip_rate_limit(ip, kind, window_start, counter) "
                    "VALUES ('10.0.0.1', %s, now(), 1)"
                )
                for kind in FOUR_KINDS:
                    cur.execute(seed, (kind,))
        except Exception:
            conn.close()
            _drop_db(psycopg2, params)
            raise
        conn.close()
    except Exception:
        _drop_db(psycopg2, params)
        raise

    yield {"db": db, "params": params}

    _drop_db(psycopg2, params)


def _drop_db(psycopg2, params):
    drop = _connect(psycopg2, "postgres", params)
    with drop.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS iprl_iso_fixture WITH (FORCE)")
    drop.close()


def _reset_to_old_constraint(cur):
    """Recreate the pre-migration (narrow) constraint on the disposable DB."""
    # Rows with widened kinds must go first or the narrow CHECK fails to add.
    cur.execute("DELETE FROM ip_rate_limit WHERE kind = 'fcm-register'")
    cur.execute("ALTER TABLE public.ip_rate_limit DROP CONSTRAINT ip_rate_limit_kind_check")
    cur.execute(
        "ALTER TABLE public.ip_rate_limit ADD CONSTRAINT ip_rate_limit_kind_check "
        "CHECK (kind IN ('query', 'mint', 'lead'))"
    )


def _restore_widened_constraint(cur):
    """Force the canonical widened target regardless of the current state."""
    cur.execute("DELETE FROM ip_rate_limit WHERE NOT kind = ANY (%s)", (list(FOUR_KINDS),))
    cur.execute(
        "ALTER TABLE public.ip_rate_limit DROP CONSTRAINT IF EXISTS ip_rate_limit_kind_check"
    )
    cur.execute(
        "ALTER TABLE public.ip_rate_limit ADD CONSTRAINT ip_rate_limit_kind_check "
        "CHECK (kind IN ('query', 'mint', 'lead', 'fcm-register'))"
    )


def test_isolated_apply_repeat_noop_and_widened(isolated_db):
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            # The bootstrap schema is already the target, so both applies must
            # take the no-op path and succeed.
            cur.execute(migration_sql)  # apply #1
            cur.execute(migration_sql)  # repeat apply #2 — safe no-op
            assert _constraint_def(cur) == TARGET_DEF

            # Widened kinds accepted, unknown kind still rejected.
            cur.execute(
                "INSERT INTO ip_rate_limit(ip, kind, window_start) "
                "VALUES ('10.0.0.2', 'fcm-register', now())"
            )
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO ip_rate_limit(ip, kind, window_start) "
                    "VALUES ('10.0.0.3', 'bogus-kind', now())"
                )
    finally:
        conn.close()


def test_isolated_widen_from_old_constraint(isolated_db):
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            # Rewind the disposable fixture to the pre-migration state.
            _reset_to_old_constraint(cur)
            assert "fcm-register" not in _constraint_def(cur)
            # Live-server proof: the actual pg_get_constraintdef(oid, true)
            # narrow rendering canonicalizes to the migration's narrow
            # shape-guard skeleton (guards against PG version rendering drift).
            assert _canonical_skeleton(_constraint_def(cur)) == NARROW_SKELETON

            cur.execute(migration_sql)  # apply #1 widens
            d1 = _constraint_def(cur)
            cur.execute(migration_sql)  # apply #2 no-op
            d2 = _constraint_def(cur)
            assert d1 == d2 == TARGET_DEF

            # Widened kinds accepted after the fix.
            cur.execute(
                "INSERT INTO ip_rate_limit(ip, kind, window_start) "
                "VALUES ('10.1.0.1', 'fcm-register', now())"
            )
    finally:
        conn.close()


def test_isolated_concurrent_applies_serialize_on_advisory_lock(isolated_db):
    """Two concurrent transactions: loser BLOCKS on the lock, then no-ops.

    Blocking is proven deterministically: while T1 holds the advisory lock
    uncommitted, T2's lock statement under a short lock_timeout must raise
    SQLSTATE 55P03 — which can only happen if the lock is genuinely held.
    Afterwards a second transaction runs the full migration to completion
    (blocked-then-completes equivalent) and the widened definition survives.
    """
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")

    # Rewind to the pre-migration narrow constraint.
    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _reset_to_old_constraint(cur)
    finally:
        setup.close()

    # T1: take the advisory lock + widen, hold uncommitted.
    t1 = _connect(psycopg2, db, params, autocommit=False)
    t2 = _connect(psycopg2, db, params)
    try:
        with t1.cursor() as cur:
            cur.execute(LOCK_STMT)
            cur.execute("ALTER TABLE public.ip_rate_limit DROP CONSTRAINT ip_rate_limit_kind_check")
            cur.execute(
                "ALTER TABLE public.ip_rate_limit ADD CONSTRAINT "
                "ip_rate_limit_kind_check CHECK (kind IN "
                "('query', 'mint', 'lead', 'fcm-register'))"
            )
        # T1 still uncommitted: advisory lock + ACCESS EXCLUSIVE held.

        # T2 must BLOCK: a short lock_timeout on the same advisory key can only
        # be exhausted if T1's uncommitted transaction holds the lock.
        with t2.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT_CANCELED,))
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cur.execute(LOCK_STMT)
            cur.execute("ROLLBACK")

        # T1 commits; its advisory lock is released with the transaction.
        t1.commit()

        # T2 (full transaction, same as production apply) now completes: it
        # observes the widened definition and no-ops instead of racing.
        with t2.cursor() as cur:
            cur.execute(migration_sql)
            assert _constraint_def(cur) == TARGET_DEF
            # The rewind removed widened-kind rows; the widened check must
            # accept them again, proving the constraint really is widened.
            cur.execute(
                "INSERT INTO ip_rate_limit(ip, kind, window_start) "
                "VALUES ('10.2.0.1', 'fcm-register', now())"
            )

        # Exactly one constraint with the target definition, data intact.
        with t2.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_constraint WHERE conname = "
                "'ip_rate_limit_kind_check' AND conrelid = "
                "'public.ip_rate_limit'::regclass"
            )
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(DISTINCT kind) FROM ip_rate_limit")
            assert cur.fetchone()[0] == 4
    finally:
        t1.close()
        t2.close()


def test_isolated_drifted_constraint_fails_closed_and_rolls_back(isolated_db):
    """Schema drift (extra allowed kind) must raise, roll back, never repair."""
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    drifted_def = (
        "CHECK (kind = ANY (ARRAY['query'::text, 'mint'::text, "
        "'lead'::text, 'fcm-register'::text, 'admin'::text]))"
    )
    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            # Plant drifted state: same shape, but one unexpected extra kind.
            cur.execute("DELETE FROM ip_rate_limit WHERE kind = 'admin'")
            cur.execute("ALTER TABLE public.ip_rate_limit DROP CONSTRAINT ip_rate_limit_kind_check")
            cur.execute(
                "ALTER TABLE public.ip_rate_limit ADD CONSTRAINT "
                "ip_rate_limit_kind_check CHECK (kind IN "
                "('query', 'mint', 'lead', 'fcm-register', 'admin'))"
            )

            with pytest.raises(Exception) as excinfo:
                with conn.cursor() as probe_cur:
                    probe_cur.execute(migration_sql)
            assert "refusing unsafe migration" in str(excinfo.value)
            assert "drift" in str(excinfo.value)
            conn.rollback()

        # Fail closed: a fresh connection still sees the drifted constraint
        # untouched — the migration neither dropped nor silently repaired it.
        probe = _connect(psycopg2, db, params)
        try:
            with probe.cursor() as cur:
                assert _constraint_def(cur) == drifted_def
                # The drifted schema still behaves as it did (probe proves the
                # migration really did not touch it).
                cur.execute(
                    "INSERT INTO ip_rate_limit(ip, kind, window_start) "
                    "VALUES ('10.3.0.1', 'admin', now())"
                )
        finally:
            probe.close()
    finally:
        conn.close()

    # Restore the canonical widened state for deterministic fixture ordering.
    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _restore_widened_constraint(cur)
    finally:
        setup.close()


def test_isolated_duplicate_kind_constraint_fails_closed(isolated_db):
    """A duplicated literal whose skeleton still matches the target shape
    must fail closed — DISTINCT collapsing would falsely accept it."""
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    # Same 4-slot shape as the target; 'query' appears twice. The skeleton
    # matches target_skeleton exactly, so only the duplicate tripwire (or
    # the non-distinct list comparison) can reject this state.
    dup_def = "CHECK (kind = ANY (ARRAY['query'::text, 'mint'::text, 'lead'::text, 'query'::text]))"
    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            # The dup constraint omits 'fcm-register', so seeded rows with that
            # kind must go first or ADD CONSTRAINT fails on existing data.
            cur.execute("DELETE FROM ip_rate_limit WHERE kind = 'fcm-register'")
            cur.execute("ALTER TABLE public.ip_rate_limit DROP CONSTRAINT ip_rate_limit_kind_check")
            cur.execute(
                "ALTER TABLE public.ip_rate_limit ADD CONSTRAINT "
                "ip_rate_limit_kind_check CHECK (kind IN "
                "('query', 'mint', 'lead', 'query'))"
            )
            assert _constraint_def(cur) == dup_def

            with pytest.raises(Exception) as excinfo:
                with conn.cursor() as probe_cur:
                    probe_cur.execute(migration_sql)
            assert "refusing unsafe migration" in str(excinfo.value)
            conn.rollback()

        # Fail closed: the duplicate-literal constraint is untouched.
        probe = _connect(psycopg2, db, params)
        try:
            with probe.cursor() as cur:
                assert _constraint_def(cur) == dup_def
        finally:
            probe.close()
    finally:
        conn.close()

    # Restore the canonical widened state for deterministic fixture ordering.
    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _restore_widened_constraint(cur)
    finally:
        setup.close()


def test_isolated_missing_constraint_fails_closed(isolated_db):
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    conn = _connect(psycopg2, db, params)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE public.ip_rate_limit DROP CONSTRAINT ip_rate_limit_kind_check")
        with pytest.raises(Exception) as excinfo:
            with conn.cursor() as cur:
                cur.execute(migration_sql)
        # Fail-closed: the DO block's explicit RAISE, surfaced as P0001.
        assert "refusing unsafe migration" in str(excinfo.value)
        # The aborted transaction left nothing behind; on a fresh connection
        # the constraint is still absent (whole migration rolled back).
        probe = _connect(psycopg2, db, params)
        try:
            with probe.cursor() as cur:
                assert _constraint_def(cur) is None
        finally:
            probe.close()
    finally:
        conn.close()

    # Restore the canonical widened state for deterministic fixture ordering.
    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _restore_widened_constraint(cur)
    finally:
        setup.close()


def test_isolated_migration_under_repeatable_read_caller(isolated_db):
    """First-statement invocation from a REPEATABLE READ caller downgrades."""
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")

    # Ensure widened state; the migration should take the no-op path.
    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _restore_widened_constraint(cur)
    finally:
        setup.close()

    conn = _connect(psycopg2, db, params, autocommit=False)
    try:
        # Non-default caller isolation: psycopg2 issues
        # BEGIN ISOLATION LEVEL REPEATABLE READ before the first statement.
        conn.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ
        with conn.cursor() as cur:
            # Migration is the very first statement of the caller's
            # transaction: its SET TRANSACTION pin downgrades the snapshot
            # discipline to READ COMMITTED instead of erroring.
            cur.execute(migration_sql)
        conn.rollback()  # nothing to persist; the script committed itself

        probe = _connect(psycopg2, db, params)
        try:
            with probe.cursor() as cur:
                assert _constraint_def(cur) == TARGET_DEF
        finally:
            probe.close()
    finally:
        conn.close()


def test_isolated_migration_mid_transaction_fails_closed(isolated_db):
    """Invoked after a query inside a caller tx: clear failure, no DDL, no
    unsafe continuation on a stale snapshot (documented behavior)."""
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")

    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _restore_widened_constraint(cur)
    finally:
        setup.close()

    conn = _connect(psycopg2, db, params, autocommit=False)
    try:
        conn.isolation_level = psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ
        with conn.cursor() as cur:
            cur.execute("SELECT 1")  # opens the snapshot; migration is no
            # longer the first statement of this transaction.
            with pytest.raises(
                (psycopg2.errors.ActiveSqlTransaction, psycopg2.errors.ProgrammingError)
            ) as excinfo:
                cur.execute(migration_sql)
            assert "isolation" in str(excinfo.value).lower()
        conn.rollback()

        probe = _connect(psycopg2, db, params)
        try:
            with probe.cursor() as cur:
                # The failed invocation changed nothing.
                assert _constraint_def(cur) == TARGET_DEF
        finally:
            probe.close()
    finally:
        conn.close()


def test_isolated_concurrent_full_migration_threads(isolated_db):
    """Two threads run the COMPLETE migration concurrently from fresh narrow
    state while a dedicated holder connection owns the advisory lock. Both
    worker backends are proven — via bounded monotonic-deadline polling of
    pg_locks / pg_stat_activity / pg_blocking_pids on the exact advisory key —
    to be queued waiting BEFORE
    the holder releases, then both complete and the final constraint must be
    exactly the four-kind target."""
    pytest.importorskip("psycopg2")

    db, params = isolated_db["db"], isolated_db["params"]
    migration_sql = MIGRATION.read_text(encoding="utf-8")
    errors: list[str] = []
    evidence: dict = {}

    # Fresh narrow pre-migration state for BOTH threads (no widened fixture).
    setup = _connect(psycopg2, db, params)
    try:
        with setup.cursor() as cur:
            _reset_to_old_constraint(cur)
            assert "fcm-register" not in _constraint_def(cur)
            cur.execute("SELECT current_setting('server_version_num')::int")
            if cur.fetchone()[0] < 90600:
                pytest.skip("PostgreSQL >= 9.6 needed for wait_event/pg_blocking_pids")
    finally:
        setup.close()

    observer = _connect(psycopg2, db, params)
    # Dedicated holder: takes the SAME advisory xact lock and holds it so
    # neither worker can pass the lock statement before we inspect state.
    holder = _connect(psycopg2, db, params, autocommit=False)
    try:
        with holder.cursor() as cur:
            cur.execute(LOCK_STMT)
            cur.execute("SELECT pg_backend_pid()")
            holder_pid = cur.fetchone()[0]

        worker_pids: list[int] = []
        pids_ready = threading.Event()
        launch = threading.Event()

        def apply_migration() -> None:
            try:
                conn = _connect(psycopg2, db, params)
                try:
                    with conn.cursor() as cur:
                        # Backend PID captured BEFORE execution.
                        cur.execute("SELECT pg_backend_pid()")
                        worker_pids.append(cur.fetchone()[0])
                    if len(worker_pids) == 2:
                        pids_ready.set()
                    if not launch.wait(timeout=30):
                        raise RuntimeError("test harness never launched worker")
                    # The COMPLETE migration: BEGIN ... COMMIT, not fragments.
                    with conn.cursor() as cur:
                        cur.execute(migration_sql)
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=apply_migration) for _ in range(2)]
        for t in threads:
            t.start()

        # Client-side coordination ONLY proves launch, not lock contention.
        assert pids_ready.wait(timeout=30), "workers never reported their PIDs"
        assert len(set(worker_pids)) == 2, f"expected 2 distinct backends: {worker_pids}"
        launch.set()

        def _both_waiters_queued() -> bool:
            # Pure server-side predicate, one catalog query per evaluation:
            # pg_locks rows for the exact advisory key (classid, objid,
            # objsubid=2 for two-int4 keys) joined to pg_stat_activity, with
            # pg_blocking_pids. Timing is owned by the bounded poll loop below.
            with observer.cursor() as cur:
                cur.execute(
                    "SELECT l.pid, l.granted, a.wait_event_type, "
                    "pg_blocking_pids(l.pid) "
                    "FROM pg_locks l "
                    "JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.classid = %s "
                    "AND l.objid = %s AND l.objsubid = 2 "
                    "AND l.pid = ANY(%s::int[])",
                    (5393038, 1, [holder_pid] + worker_pids),
                )
                rows = cur.fetchall()
            evidence["rows"] = rows
            by_pid = {row[0]: row for row in rows}
            holder_row = by_pid.get(holder_pid)
            if holder_row is None or not holder_row[1]:
                return False
            for wpid in worker_pids:
                row = by_pid.get(wpid)
                # Proves each worker: (a) a NOT-GRANTED lock request queued on
                # the exact advisory key, (b) inside a Lock wait event, and
                # (c) blocked by the holder per pg_blocking_pids. A waiter
                # queued behind the other worker still lists the holder.
                if row is None or row[1] or row[2] != "Lock" or holder_pid not in row[3]:
                    return False
            return True

        # Deterministic bounded condition polling: each iteration evaluates the
        # server-side predicate above and, while it is still false, waits a
        # short interval (monotonic deadline enforced) before the next catalog
        # read. This is lock-state observation only — the migration SQL itself
        # is never retried, and no worker notify is required because the
        # observer drives the loop. If the deadline expires without proof, the
        # holder is still released (finally/rollback below) and the last
        # observed lock rows remain in `evidence` for the failure message.
        poll_interval = 0.05
        poll_event = threading.Event()
        deadline = time.monotonic() + 30.0
        proven = _both_waiters_queued()
        while not proven and time.monotonic() < deadline:
            poll_event.wait(poll_interval)
            proven = _both_waiters_queued()
        if not proven:
            # Diagnostic snapshot on timeout: per-backend activity state so the
            # failure message shows what each connection was actually doing.
            with observer.cursor() as cur:
                cur.execute(
                    "SELECT pid, state, wait_event_type, wait_event, "
                    "left(query, 60) FROM pg_stat_activity "
                    "WHERE pid = ANY(%s::int[])",
                    ([holder_pid] + worker_pids,),
                )
                evidence["activity_on_timeout"] = cur.fetchall()

        # Serialization is released ONLY after both backends were observed
        # queued on the held advisory lock.
        holder.rollback()

        for t in threads:
            t.join(timeout=60)
        assert not any(t.is_alive() for t in threads), "concurrent apply hung"
        assert proven, (
            "lock-state proof failed: not both migration backends were seen "
            f"waiting on the advisory lock before release; evidence={evidence}"
        )
        assert errors == [], f"concurrent apply failed: {errors}"

        conn = _connect(psycopg2, db, params)
        try:
            with conn.cursor() as cur:
                # Exact final definition: canonical four-kind membership check.
                assert _constraint_def(cur) == TARGET_DEF
                # Exactly one constraint object (no duplicate replacements).
                cur.execute(
                    "SELECT count(*) FROM pg_constraint WHERE conname = "
                    "'ip_rate_limit_kind_check' AND conrelid = "
                    "'public.ip_rate_limit'::regclass"
                )
                assert cur.fetchone()[0] == 1
                # All four kinds accepted end to end.
                for idx, kind in enumerate(FOUR_KINDS):
                    cur.execute(
                        "INSERT INTO ip_rate_limit(ip, kind, window_start) "
                        f"VALUES ('10.4.0.{idx}', %s, now())",
                        (kind,),
                    )
                cur.execute("SELECT count(DISTINCT kind) FROM ip_rate_limit")
                assert cur.fetchone()[0] == 4
        finally:
            conn.close()
    finally:
        holder.close()
        observer.close()
