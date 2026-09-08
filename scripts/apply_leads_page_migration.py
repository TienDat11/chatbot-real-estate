"""One-off local application + verification of the leads-page index migration.

Not part of the test suite; run manually:
    .venv/Scripts/python.exe -X utf8 scripts/apply_leads_page_migration.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import asyncpg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from api.infrastructure.config.config import get_settings  # noqa: E402

MIGRATION = pathlib.Path("db/migrations/2026-09-03-crm-leads-page.sql")


async def main() -> None:
    dsn = get_settings().pg_dsn_sync
    sql = MIGRATION.read_text()
    # Drop full-line comments, then split on ';'. (A naive split fails when a
    # comment contains a ';' — e.g. the header's purpose text.)
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        for statement in statements:
            await conn.execute(statement)
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'leads' "
            "AND indexname IN ('idx_leads_crm_page', 'idx_leads_created_desc') ORDER BY 1"
        )
        print("indexes:", [row["indexname"] for row in rows])
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
