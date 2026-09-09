"""Static contract checks for the forward-only G3-r6 unified sales workspace migration.

These tests parse the migration SQL directly so they run anywhere; the
authoritative apply/repeat/rollback proof is
tests/test_g3_r6_migration.py::test_isolated_apply_repeat (requires a local
PostgreSQL 16.6+ with pgvector, see _admin_params).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = REPO / "db" / "migrations" / "2026-08-28-unified-sales-workspace.sql"

# Dollar-quote tags ($$ or $tag$) protect ';' inside DO-block bodies.
_DOLLAR_TAG = re.compile(r"\$\$|\$[A-Za-z_][A-Za-z0-9_]*\$")


@dataclass(frozen=True)
class SqlStatement:
    kind: str  # 'BEGIN' | 'COMMIT' | 'DDL' | 'other'
    text: str

    @property
    def sql(self) -> str:
        return self.text


def _consume_string(sql: str, i: int) -> int:
    """Return the index just past a single-quoted literal starting at sql[i]."""
    j = i + 1
    n = len(sql)
    while j < n:
        if sql[j] == "'":
            if j + 1 < n and sql[j + 1] == "'":
                j += 2  # escaped quote
                continue
            return j + 1
        j += 1
    return n


def split_statements(sql: str) -> list[SqlStatement]:
    """Split into top-level statements.

    Single-pass state machine that skips -- comments, skips over single-quoted
    literals (so ';' inside a COMMENT string does not split), and honours
    dollar-quote bodies (so ';' inside DO blocks does not split).
    """
    out: list[SqlStatement] = []
    buf: list[str] = []
    tag: str | None = None
    i = 0
    n = len(sql)
    while i < n:
        if tag is not None:
            if sql[i] == "$" and sql.startswith(tag, i):
                buf.append(tag)
                i += len(tag)
                tag = None
                continue
            buf.append(sql[i])
            i += 1
            continue
        if sql[i : i + 2] == "--":
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl
            continue
        ch = sql[i]
        if ch == "'":
            end = _consume_string(sql, i)
            buf.append(sql[i:end])
            i = end
            continue
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                buf.append(tag)
                i = m.end()
                continue
        if ch == ";":
            _flush(out, "".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    _flush(out, "".join(buf))
    return out


def _flush(out: list[SqlStatement], raw: str) -> None:
    text = raw.strip()
    if not text:
        return
    upper = text.upper()
    if upper.startswith("BEGIN"):
        kind = "BEGIN"
    elif upper.startswith("COMMIT"):
        kind = "COMMIT"
    elif "ALTER TABLE" in upper or "CREATE" in upper:
        kind = "DDL"
    else:
        kind = "other"
    out.append(SqlStatement(kind, text))


def migration_text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def ddl_statements() -> list[str]:
    return [s.sql for s in split_statements(migration_text()) if s.kind == "DDL"]


def _norm(text: str) -> str:
    """Whitespace-insensitive comparison form for DDL fragments."""
    return "".join(text.lower().split())


def find_ddl(fragment: str) -> list[str]:
    frag = _norm(fragment)
    return [s for s in ddl_statements() if frag in _norm(s)]


def has_ddl(fragment: str) -> bool:
    return bool(find_ddl(fragment))


def has_do_block(fragment: str) -> bool:
    frag = _norm(fragment)
    return any(
        stmt.sql.upper().startswith("DO") and frag in _norm(stmt.sql)
        for stmt in split_statements(migration_text())
        if stmt.kind == "DDL"
    )


def ddl_without_do_bodies() -> list[str]:
    """Top-level DDL text with DO bodies excised (for destructive-literal checks)."""
    out: list[str] = []
    for stmt in split_statements(migration_text()):
        if stmt.kind != "DDL":
            continue
        text = stmt.sql
        if text.upper().startswith("DO"):
            # keep only the wrapper header; the guarded constraint body is read
            # via find_ddl()/has_do_block() instead.
            out.append("DO")
        else:
            out.append(text)
    return out
