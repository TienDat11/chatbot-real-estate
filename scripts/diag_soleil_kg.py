"""Read-only diagnostic: Soleil KG (entity/relation) completeness audit.

Adapted from scripts/diag_soleil_ids.py for the KG-extraction completion audit.
Pure SELECT diagnostics + read-only retrieval probes (run_rag_leg). NEVER writes:
no INSERT/UPDATE/DELETE/DDL, no docker/server lifecycle actions. Credentials come
from app Settings; never printed.

Sections:
  A. ragre_mvp workspace totals: vdb_entity / vdb_relation / graph_nodes /
     graph_edges (+ chunks + doc_status for context).
  B. Entity/relation attribution per Soleil registry doc (vdb file_path prefix
     match ``<doc_id>:*`` plus a null-file_path bucket).
  C. Default workspace (Camellia) parity check: expected 174 chunks / 1717
     entities / 1690 relations.
  D. (optional, --probes) Retrieval probes via run_rag_leg(project_key='soleil').

Usage:
  python scripts/diag_soleil_kg.py [--probes]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WS = "ragre_mvp"
VDB_CHUNKS = "lightrag_vdb_chunks_gemini_embedding_001_1024d"
VDB_ENTITY = "lightrag_vdb_entity_gemini_embedding_001_1024d"
VDB_RELATION = "lightrag_vdb_relation_gemini_embedding_001_1024d"

# Soleil registry doc ids (documents.project_key = 'soleil').
SOLEIL_DOCS = (
    "legal-soleil-chu-truong-2018",
    "legal-soleil-pccc-2017",
    "legal-soleil-qd6608-2016",
    "price-soleil-2026q3",
    "price-soleil-2026q3-payment",
    "price-soleil-2026q3-policy",
    "project-soleil-2026q3",
    "project-soleil-qna",
    "rental-soleil-2026q3",
)

# KG-extraction audit targets (were zero-entity before the retry ingest).
TARGET_DOCS = (
    "legal-soleil-pccc-2017",
    "legal-soleil-qd6608-2016",
    "price-soleil-2026q3",
    "rental-soleil-2026q3",
)

# Default (Camellia) workspace parity expectations — must stay unchanged.
CAMELLIA_EXPECTED = {"chunks": 174, "entities": 1717, "relations": 1690}


async def section_totals(conn) -> dict[str, int]:
    print("\n=== A. ragre_mvp workspace totals ===")
    queries = {
        "vdb_entity": f"SELECT count(*) FROM {VDB_ENTITY} WHERE workspace = $1",
        "vdb_relation": f"SELECT count(*) FROM {VDB_RELATION} WHERE workspace = $1",
        "vdb_chunks": f"SELECT count(*) FROM {VDB_CHUNKS} WHERE workspace = $1",
        "graph_nodes": "SELECT count(*) FROM lightrag_graph_nodes WHERE workspace = $1",
        "graph_edges": "SELECT count(*) FROM lightrag_graph_edges WHERE workspace = $1",
        "doc_status": "SELECT count(*) FROM lightrag_doc_status WHERE workspace = $1",
    }
    totals: dict[str, int] = {}
    for name, sql in queries.items():
        n = await conn.fetchval(sql, WS)
        totals[name] = int(n)
        print(f"  {name} = {n}")
    return totals


async def section_attribution(conn) -> dict[str, tuple[int, int]]:
    print("\n=== B. Entity/relation attribution per Soleil doc (ragre_mvp) ===")
    print(f"  {'doc_id':34} {'entities':>9} {'relations':>10}")
    results: dict[str, tuple[int, int]] = {}
    for doc in SOLEIL_DOCS:
        ents = await conn.fetchval(
            f"SELECT count(*) FROM {VDB_ENTITY} WHERE workspace = $1 AND file_path LIKE $2",
            WS,
            doc + ":%",
        )
        rels = await conn.fetchval(
            f"SELECT count(*) FROM {VDB_RELATION} WHERE workspace = $1 AND file_path LIKE $2",
            WS,
            doc + ":%",
        )
        results[doc] = (int(ents), int(rels))
        print(f"  {doc:34} {ents:>9} {rels:>10}")
    null_e = await conn.fetchval(
        f"SELECT count(*) FROM {VDB_ENTITY} WHERE workspace = $1 AND file_path IS NULL", WS
    )
    null_r = await conn.fetchval(
        f"SELECT count(*) FROM {VDB_RELATION} WHERE workspace = $1 AND file_path IS NULL", WS
    )
    print(f"  (unattributed: entities={null_e} relations={null_r})")
    return results


async def section_camellia(conn) -> None:
    print("\n=== C. Default workspace (Camellia) parity check ===")
    checks = {
        "chunks": f"SELECT count(*) FROM {VDB_CHUNKS} WHERE workspace = 'default'",
        "entities": f"SELECT count(*) FROM {VDB_ENTITY} WHERE workspace = 'default'",
        "relations": f"SELECT count(*) FROM {VDB_RELATION} WHERE workspace = 'default'",
    }
    ok = True
    for name, sql in checks.items():
        n = int(await conn.fetchval(sql))
        want = CAMELLIA_EXPECTED[name]
        status = "OK" if n == want else "MISMATCH"
        if n != want:
            ok = False
        print(f"  default/{name} = {n} (expected {want}) -> {status}")
    print(f"  camellia_unchanged = {ok}")


async def section_probes() -> None:
    print("\n=== D. Retrieval probes (run_rag_leg, project_key='soleil') ===")
    from api.application.services.rag_leg import run_rag_leg

    probes = {
        "pccc": "Soleil quy định phòng cháy chữa cháy như thế nào?",
        "rental": "Giá thuê căn hộ Soleil bao nhiêu một tháng?",
    }
    for tag, q in probes.items():
        result = await run_rag_leg(q, [], [], date.today(), project_key="soleil")
        chunks = result.chunks or []
        ids = [c.get("id") for c in chunks[:6]]
        src_docs = sorted({str(c.get("id", "")).rsplit(":", 2)[0] for c in chunks if c.get("id")})
        print(f"\n[{tag}] {q}")
        print(f"  chunks={len(chunks)} degraded={result.degraded} "
              f"reasons={result.degraded_reasons} error={result.error}")
        print(f"  first6_ids={ids}")
        print(f"  source_doc_ids={src_docs}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Soleil KG completeness audit (read-only).")
    ap.add_argument("--probes", action="store_true", help="Also run retrieval probes.")
    args = ap.parse_args()

    import asyncpg

    from api.infrastructure.config.config import get_settings

    conn = await asyncpg.connect(get_settings().pg_dsn)
    try:
        await section_totals(conn)
        await section_attribution(conn)
        await section_camellia(conn)
    finally:
        await conn.close()
    if args.probes:
        await section_probes()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
