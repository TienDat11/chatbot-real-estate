"""Static contract checks for the forward-only quota migration."""

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "2026-08-24-anon-quota-and-ip-rate-limit.sql"
)


def test_quota_migration_is_forward_only_and_never_destructive():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "drop table" not in sql
    assert "drop schema" not in sql
    assert "truncate" not in sql
    # A legacy table must be inspected before ALTERs, so the migration uses a
    # guarded CREATE TABLE inside a transactional DO block rather than the
    # simpler CREATE TABLE IF NOT EXISTS form.
    assert "if to_regclass('public.anon_quota') is null then" in sql
    assert "create table public.anon_quota" in sql
    assert "alter table public.anon_quota add column if not exists project_key" in sql
    assert "refusing unsafe migration" in sql


def test_quota_migration_preserves_all_quota_state_columns():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for column in (
        "identity_key",
        "project_key",
        "used_turns",
        "bonus_turns",
        "granted_turns",
        "bonus_granted",
        "created_at",
        "updated_at",
    ):
        assert column in sql


def test_quota_migration_has_idempotent_objects_and_no_provider_literals():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "if not exists" in sql
    assert "openrouter" not in sql
    assert "stealth" not in sql
    assert "ox-alpha" not in sql
    assert "vector(" not in sql


def test_schema_describes_post_migration_quota_shape():
    sql = (
        (Path(__file__).resolve().parents[1] / "db" / "schema.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create table if not exists anon_quota" in sql
    assert "primary key (identity_key, project_key)" in sql
    assert "create table if not exists ip_rate_limit" in sql
