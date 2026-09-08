"""Read-only diagnostic: Soleil P1 workspace-routing verification evidence.

Pure SELECT diagnostics + read-only LightRAG query probes (aquery via run_rag_leg
with only_need_context=True). NEVER writes: no INSERT/UPDATE/DELETE/DDL, no
docker/server lifecycle actions. Credentials come from app Settings; never printed.

Sections:
  A. Per-workspace row counts for all 6 LightRAG tables (DISTINCT workspace).
  B. Registry parity: document_chunks JOIN documents grouped by project_key,
     Soleil doc id/version list.
  C. VDB chunk-id prefix classification per workspace (which project docs live
     where) — the doc id convention is ``<kind>-<project>-<slug>:<version>:<idx>``.
  D. Attribution evidence: lightrag_doc_status rows per workspace (ids +
     created_at/updated_at), focused on the two retried Soleil doc ids, plus
     ingest_log + documents.updated_at for the same docs.
  E. Retrieval probes via run_rag_leg (soleil + camellia benchmark).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLES = (
    "lightrag_vdb_chunks_gemini_embedding_001_1024d",
    "lightrag_vdb_entity_gemini_embedding_001_1024d",
    "lightrag_vdb_relation_gemini_embedding_001_1024d",
    "lightrag_graph_nodes",
    "lightrag_graph_edges",
    "lightrag_doc_status",
)

# Second token of the doc_id convention (validated against load.py's known set).
KNOWN_PROJECTS = ("soleil", "camellia")


def doc_prefix(chunk_id: str) -> str:
    """Registry doc id from a LightRAG chunk id ``<doc_id>:<version>:<idx>``."""
    parts = chunk_id.rsplit(":", 2)
    return parts[0] if len(parts) == 3 else chunk_id


def project_of(doc_id: str) -> str:
    tokens = doc_id.split("-")
    return tokens[1] if len(tokens) >= 2 and tokens[1] in KNOWN_PROJECTS else "other"


async def section_counts(conn) -> None:
    print("\n=== A. Per-workspace row counts (GROUP BY workspace) ===")
    for table in TABLES:
        rows = await conn.fetch(
            f"SELECT workspace, count(*) AS n FROM {table} GROUP BY workspace ORDER BY workspace"
        )
        print(f"{table}: " + (", ".join(f"{r['workspace']!r}={r['n']}" for r in rows) or "(empty)"))


async def section_registry(conn) -> None:
    print("\n=== B. Registry parity (documents JOIN document_chunks) ===")
    rows = await conn.fetch(
        """
        SELECT d.project_key, count(DISTINCT d.doc_id) AS docs, count(*) AS chunks
        FROM document_chunks c JOIN documents d ON d.doc_id = c.doc_id
        GROUP BY d.project_key ORDER BY d.project_key NULLS FIRST
        """
    )
    for r in rows:
        print(f"project_key={r['project_key']!r}: docs={r['docs']} chunks={r['chunks']}")

    print("\n-- Soleil documents (doc_id, version, status, kind, effective, updated_at) --")
    rows = await conn.fetch(
        """
        SELECT doc_id, version, status, kind, effective_from, effective_to, updated_at
        FROM documents WHERE project_key = $1 ORDER BY doc_id
        """,
        "soleil",
    )
    for r in rows:
        print(
            f"  {r['doc_id']} v{r['version']} {r['status']} {r['kind']} "
            f"[{r['effective_from']}..{r['effective_to']}] upd={r['updated_at']}"
        )
    print(f"  total_soleil_docs={len(rows)}")


async def section_vdb_prefixes(conn) -> None:
    print("\n=== C. VDB chunk-id doc-prefix classification per workspace ===")
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name=$1",
        "lightrag_vdb_chunks_gemini_embedding_001_1024d",
    )
    id_col = "id" if any(c["column_name"] == "id" for c in cols) else "chunk_id"
    rows = await conn.fetch(
        f"SELECT workspace, {id_col} AS cid FROM lightrag_vdb_chunks_gemini_embedding_001_1024d"
    )
    by_ws: dict[str, dict[str, set[str]]] = {}
    for r in rows:
        ws = r["workspace"] if r["workspace"] is not None else "default"
        p = doc_prefix(r["cid"])
        by_ws.setdefault(ws, {}).setdefault(project_of(p), set()).add(p)
    for ws, by_proj in sorted(by_ws.items()):
        total = sum(len(v) for v in by_proj.values())
        print(f"workspace={ws!r}: distinct_doc_prefixes={total}")
        for proj, docs in sorted(by_proj.items()):
            print(f"  project={proj}: doc_count={len(docs)} -> {sorted(docs)[:12]}")


async def section_attribution(conn) -> None:
    print("\n=== D. Attribution evidence (doc_status + ingest_log + documents) ===")
    ds_cols = {c["column_name"] for c in await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name=$1",
        "lightrag_doc_status",
    )}
    ts_cols = ", ".join(c for c in ("created_at", "updated_at") if c in ds_cols) or "NULL"
    id_col = "id" if "id" in ds_cols else "doc_id"
    rows = await conn.fetch(
        f"SELECT workspace, count(*) AS n FROM lightrag_doc_status GROUP BY workspace"
    )
    print("doc_status per workspace: " + ", ".join(f"{r['workspace']!r}={r['n']}" for r in rows))

    for ws in ("ragre_mvp", "default", None):
        cond = "workspace = $1" if ws is not None else "workspace IS NULL"
        arg: tuple = (ws,) if ws is not None else ()
        rows = await conn.fetch(
            f"SELECT {id_col} AS did, {ts_cols} FROM lightrag_doc_status WHERE {cond} ORDER BY did LIMIT 12",
            *arg,
        )
        n = await conn.fetchval(f"SELECT count(*) FROM lightrag_doc_status WHERE {cond}", *arg)
        print(f"\nworkspace={ws!r}: doc_status rows={n} (sample up to 12)")
        for r in rows:
            print(f"  {r['did']} created={r.get('created_at')} updated={r.get('updated_at')}")

    print("\n-- Two retried Soleil doc ids: doc_status rows by workspace --")
    for doc in ("project-soleil-2026q3", "project-soleil-qna"):
        rows = await conn.fetch(
            f"""SELECT workspace, {id_col} AS did, status, {ts_cols}
                FROM lightrag_doc_status WHERE {id_col} LIKE $1 ORDER BY workspace, did""",
            doc + ":%",
        )
        print(f"doc {doc}: doc_status rows={len(rows)}")
        for r in rows[:8]:
            print(f"  ws={r['workspace']!r} {r['did']} status={r['status']} "
                  f"created={r.get('created_at')} updated={r.get('updated_at')}")
        docs = await conn.fetch(
            "SELECT doc_id, version, updated_at FROM documents WHERE doc_id = $1", doc
        )
        for d in docs:
            print(f"  registry: v{d['version']} updated_at={d['updated_at']}")
        logs = await conn.fetch(
            """SELECT action, version, chunk_count, detail, created_at FROM ingest_log
               WHERE doc_id = $1 ORDER BY created_at""",
            doc,
        )
        for l in logs:
            print(f"  ingest_log: {l['action']} v{l['version']} chunks={l['chunk_count']} "
                  f"at={l['created_at']} detail={l['detail']}")


async def section_probes() -> None:
    print("\n=== E. Retrieval probes (run_rag_leg, only_need_context=True) ===")
    from api.application.services.rag_leg import run_rag_leg

    probes = {
        "soleil": "Soleil có những loại căn hộ nào, diện tích từng loại bao nhiêu?",
        "camellia": "Camellia có những loại căn hộ nào, diện tích từng loại bao nhiêu?",
    }
    for project, q in probes.items():
        result = await run_rag_leg(q, [], [], date.today(), project_key=project)
        chunks = result.chunks or []
        ids = [c.get("id") for c in chunks[:5]]
        src_docs = sorted({doc_prefix(c["id"]) for c in chunks if c.get("id")})
        print(f"\n[{project}] chunks={len(chunks)} degraded={result.degraded} "
              f"reasons={result.degraded_reasons} error={result.error}")
        print(f"  first5_ids={ids}")
        print(f"  source_doc_ids={src_docs}")
        soleilish = sum(1 for i in (c.get("id") or "" for c in chunks) if "soleil" in i)
        camelliish = sum(1 for i in (c.get("id") or "" for c in chunks) if "camellia" in i)
        print(f"  id_prefix_counts: soleil-substr={soleilish} camellia-substr={camelliish}")


async def main() -> int:
    import asyncpg

    from api.infrastructure.config.config import get_settings

    conn = await asyncpg.connect(get_settings().pg_dsn)
    try:
        await section_counts(conn)
        await section_registry(conn)
        await section_vdb_prefixes(conn)
        await section_attribution(conn)
    finally:
        await conn.close()
    await section_probes()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
