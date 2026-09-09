"""Contract tests for the fail-closed Soleil LightRAG scope repair."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATION = (REPO / "db/migrations/2026-08-28-soleil-lightrag-scope-repair.sql").read_text(
    encoding="utf-8"
)
VERIFIER = (REPO / "scripts/verify_soleil_scope.py").read_text(encoding="utf-8")


def test_failed_status_requires_complete_current_document_parity():
    assert "s.status = 'failed'" in MIGRATION
    assert "JOIN complete_docs cd ON cd.doc_id = cc.doc_id" in MIGRATION
    assert "vector_dims(v.content_vector) = 1024" in MIGRATION
    assert "d.project_key = 'soleil'" in MIGRATION
    assert "c.chunk_id LIKE '%-soleil-%'" in MIGRATION


def test_incomplete_failed_rows_are_not_blindly_processed():
    status_update = MIGRATION.split("UPDATE lightrag_doc_status s", 1)[1]
    assert "status = 'processed'" in status_update
    assert "AND s.status = 'failed'" in status_update
    assert "RAISE EXCEPTION 'Soleil LightRAG document parity incomplete" in MIGRATION
    assert "status <> 'processed'" in MIGRATION


def test_scope_repair_is_targeted_and_non_destructive():
    assert "LIKE '%-soleil-%'" in MIGRATION
    lowered = MIGRATION.lower()
    assert "truncate" not in lowered
    assert "delete from" not in lowered
    assert "re-embed" not in "\n".join(
        line for line in lowered.splitlines() if not line.lstrip().startswith("--")
    )


def test_verifier_reports_document_parity_and_1024_vectors():
    assert '"incomplete_documents"' in VERIFIER
    assert "vector_dims(v.content_vector) = 1024" in VERIFIER
    assert 'values["incomplete_documents"] == 0' in VERIFIER
    assert "SOLEIL_SCOPE_VERIFIER=" in VERIFIER
