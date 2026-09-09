"""Static contract checks for the call-notified stamp migration (FR-33).

Parses db/migrations/2026-09-03-lead-call-notified-stamp.sql directly so the
guards run anywhere; no database required. The migration is additive-only and
repeat-safe (ADD COLUMN IF NOT EXISTS, nullable) — re-running it must be a
no-op and no existing column/index can be dropped or rebuilt.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "2026-09-03-lead-call-notified-stamp.sql"


def migration_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def statements() -> list[str]:
    # Single-quoted literals are absent from this migration; a plain split on
    # top-level semicolons after stripping comments is sufficient and keeps the
    # contract readable.
    stripped = re.sub(r"--[^\n]*", "", migration_text())
    return [s.strip() for s in stripped.split(";") if s.strip()]


def test_migration_is_wrapped_in_a_single_transaction() -> None:
    code = re.sub(r"--[^\n]*", "", migration_text()).strip()
    begins = [s for s in statements() if s.upper().startswith("BEGIN")]
    commits = [s for s in statements() if s.upper().startswith("COMMIT")]
    assert len(begins) == 1 and len(commits) == 1
    # BEGIN comes first, COMMIT last: the whole change is atomic.
    assert code.upper().startswith("BEGIN")
    assert code.upper().endswith("COMMIT;")


def test_migration_is_repeat_safe_additive_only() -> None:
    # Executable code only (the documented down-note mentions DROP COLUMN on
    # purpose); comments are stripped before the destructive-literal checks.
    code = re.sub(r"--[^\n]*", "", migration_text())
    # Idempotent re-apply: IF NOT EXISTS on the only DDL statement.
    assert re.search(
        r"ALTER\s+TABLE\s+leads\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+call_notified_at",
        code,
        re.IGNORECASE,
    )
    upper = code.upper()
    # No drops or rebuilds — the stamp column is pure addition.
    assert "DROP" not in upper
    assert "CREATE TABLE" not in upper
    assert "CREATE INDEX" not in upper


def test_new_column_is_nullable_timestamptz() -> None:
    text = migration_text()
    assert re.search(r"call_notified_at\s+TIMESTAMPTZ", text, re.IGNORECASE)
    # Nullable: legacy leads have never been called; NOT NULL would force a
    # default backfill and break the insert path.
    assert "NOT NULL" not in text.upper()


def test_migration_does_not_touch_embedding_or_vector_objects() -> None:
    upper = migration_text().upper()
    assert "VECTOR" not in upper
    assert "EMBEDDING" not in upper


def test_migration_documents_the_down_path() -> None:
    text = migration_text()
    # Forward-only convention: the reversal SQL is documented in a comment so
    # an operator can undo the additive column without a paired file.
    down_marker = re.search(r"--\s*down:?(.*)", text, re.IGNORECASE)
    assert down_marker is not None
    assert "ALTER TABLE leads DROP COLUMN" in down_marker.group(1).upper() or (
        "DROP COLUMN" in text.upper()
    )
