"""Read-only corpus/workspace diagnostics for the LightRAG PG tables.

WHY: the Camellia smoke query reported ~40 missing chunk ids (Soleil-prefixed)
and a query-time embedding failure. This script answers, with evidence:
1. which workspaces exist per lightrag table and their row counts
2. whether the reported missing chunk ids exist in the OLD vdb chunks table
3. which doc-status rows are marked done vs. how many vdb chunks they produced
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLD = "lightrag_vdb_chunks_text_embedding_v4_1024d"
NEW = "lightrag_vdb_chunks_gemini_embedding_001_1024d"

MISSING_SAMPLES = [
    "legal-soleil-chu-truong-2018:3:1-chunk-000",
    "rental-soleil-2026q3:3:4-chunk-000",
    "project-soleil-qna:2:2-chunk-000",
    "project-soleil-qna:2:91-chunk-000",
    "price-soleil-2026q3:4:5-chunk-000",
]


async def main() -> int:
    import asyncpg

    from api.infrastructure.config.config import get_settings

    conn = await asyncpg.connect(get_settings().pg_dsn)
    try:
        for table in (OLD, NEW, "lightrag_vdb_entity_gemini_embedding_001_1024d",
                      "lightrag_vdb_relation_gemini_embedding_001_1024d"):
            rows = await conn.fetch(
                f"SELECT workspace, count(*) AS n FROM {table} GROUP BY workspace ORDER BY workspace"
            )
            print(f"{table}: " + ", ".join(f"{r['workspace']}={r['n']}" for r in rows))

        print("\nmissing-sample presence in OLD chunks table:")
        for cid in MISSING_SAMPLES:
            n_old = await conn.fetchval(f"SELECT count(*) FROM {OLD} WHERE id=$1", cid)
            n_new = await conn.fetchval(f"SELECT count(*) FROM {NEW} WHERE id=$1", cid)
            print(f"  {cid}: old={n_old} new={n_new}")

        total_docs = await conn.fetchval("SELECT count(*) FROM lightrag_doc_status")
        done_docs = await conn.fetchval(
            "SELECT count(*) FROM lightrag_doc_status WHERE status='processed'"
        )
        print(f"\ndoc_status: total={total_docs} processed={done_docs}")

        per_ws = await conn.fetch(
            "SELECT workspace, count(*) AS n FROM lightrag_doc_status GROUP BY workspace ORDER BY workspace"
        )
        print("doc_status per workspace: " + ", ".join(f"{r['workspace']}={r['n']}" for r in per_ws))
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
