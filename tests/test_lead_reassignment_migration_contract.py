"""Static contract checks for the atomic lead reassignment migration."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION = REPO_ROOT / "db" / "migrations" / "2026-08-26-atomic-lead-reassignment.sql"
LEAD_SCHEMA = REPO_ROOT / "db" / "lead_schema.sql"


def test_reassignment_migration_is_forward_only_and_never_destructive():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "drop table" not in sql
    assert "drop schema" not in sql
    assert "drop index" not in sql
    assert "truncate" not in sql
    assert "delete from" not in sql
    # DML guard targets actual table mutations, not the "SELECT ... for update"
    # row-lock comment that documents the application-side transaction.
    assert "update sales_assignment_log" not in sql
    assert "update leads" not in sql


def test_reassignment_migration_is_idempotent():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert sql.count("create index if not exists") >= 2


def test_reassignment_migration_creates_the_supporting_indexes():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for index in ("idx_sal_log_lead_sales", "idx_sal_sales_assign_recency"):
        assert index in sql


def test_reassignment_migration_has_no_value_interpolation():
    # The adapter drives the atomic decision with parameterized statements;
    # the migration must contain no literal lead/sales identifiers.
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "sales_id =" not in sql
    assert "lead_id =" not in sql


def test_lead_schema_carries_the_same_reassignment_indexes_for_fresh_installs():
    # House convention: migrations are the forward-only path for existing DBs
    # and the cumulative lead_schema.sql must carry the same shape so fresh
    # installs converge (2026-08-22 / 2026-08-23 migrations document this).
    sql = LEAD_SCHEMA.read_text(encoding="utf-8").lower()
    for index in ("idx_sal_log_lead_sales", "idx_sal_sales_assign_recency"):
        assert "create index if not exists " + index in sql