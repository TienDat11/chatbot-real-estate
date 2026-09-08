"""Read-only verifier for Soleil registry and LightRAG scope parity."""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.infrastructure.config.config import get_settings  # noqa: E402

QUERIES = {
    "documents": (
        "SELECT count(*) FROM documents WHERE doc_id LIKE '%%-soleil-%%' "
        "AND project_key = 'soleil'"
    ),
    "chunks": (
        "SELECT count(*) FROM lightrag_doc_chunks WHERE id LIKE '%%-soleil-%%' "
        "AND workspace = %s"
    ),
    "vectors": (
        "SELECT count(*) FROM lightrag_vdb_chunks_text_embedding_v4_1024d "
        "WHERE id LIKE '%%-soleil-%%' AND workspace = %s "
        "AND vector_dims(content_vector) = 1024"
    ),
    "status": (
        "SELECT count(*) FROM lightrag_doc_status WHERE id LIKE '%%-soleil-%%' "
        "AND workspace = %s AND metadata->>'project_key' = 'soleil' "
        "AND status = 'processed'"
    ),
    "bad_status": (
        "SELECT count(*) FROM lightrag_doc_status WHERE id LIKE '%%-soleil-%%' "
        "AND (workspace IS DISTINCT FROM %s OR metadata->>'project_key' "
        "IS DISTINCT FROM 'soleil' OR status <> 'processed')"
    ),
    "incomplete_documents": """
        SELECT count(*) FROM (
          SELECT d.doc_id
          FROM documents d JOIN document_chunks c ON c.doc_id = d.doc_id
          WHERE d.doc_id LIKE '%%-soleil-%%' AND d.project_key = 'soleil'
          GROUP BY d.doc_id
          HAVING count(*) <> count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM lightrag_doc_status s
            WHERE s.id = c.chunk_id OR s.id LIKE c.chunk_id || '-chunk-%%'))
             OR count(*) <> count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM lightrag_doc_chunks lc
            WHERE lc.id = c.chunk_id OR lc.id LIKE c.chunk_id || '-chunk-%%'))
             OR count(*) <> count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM lightrag_vdb_chunks_text_embedding_v4_1024d v
            WHERE (v.id = c.chunk_id OR v.id LIKE c.chunk_id || '-chunk-%%')
              AND v.workspace = %s AND vector_dims(v.content_vector) = 1024))
        ) incomplete
    """,
}


def main() -> int:
    s = get_settings()
    conn = psycopg2.connect(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=s.postgres_password,
        dbname=s.postgres_database,
        connect_timeout=8,
    )
    try:
        with conn.cursor() as cur:
            values = {}
            for name, query in QUERIES.items():
                cur.execute(query, () if name == "documents" else (s.lightrag_workspace,))
                values[name] = int(cur.fetchone()[0])
        print("SOLEIL_SCOPE", " ".join(f"{k}={v}" for k, v in values.items()))
        ok = (
            values["documents"] > 0
            and values["chunks"] == values["vectors"] == values["status"]
            and values["bad_status"] == 0
            and values["incomplete_documents"] == 0
        )
        print("SOLEIL_SCOPE_VERIFIER=" + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
