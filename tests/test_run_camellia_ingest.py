"""Unit tests for ingest.run_camellia_ingest (Story 2.3 driver) — pure planning logic.

The driver's live path (Postgres + LLM extraction + LightRAG ainsert) is verified
against the real stack via scripts/verify_ingest.sql and an idempotent re-run;
these tests pin down the seed-preservation contract without any network or DB.
"""

from ingest.run_camellia_ingest import PRESERVE_SEED_FACTS, plan_doc_ingest


def test_seed_carrier_skips_extraction_and_preserves_seed_facts():
    assert PRESERVE_SEED_FACTS == {"price-camellia-2026q3"}
    assert plan_doc_ingest("price-camellia-2026q3") == (False, True)


def test_all_other_registry_docs_extract_without_preserving():
    for doc_id in (
        "price-camellia-2026q3-payment",
        "price-camellia-2026q3-policy",
        "project-camellia-2026q3",
        "project-camellia-qna",
        "legal-gcnqsd-2011",
        "legal-qd254-2024",
        "legal-qd191-2025",
        "legal-cv12779-2026",
    ):
        assert plan_doc_ingest(doc_id) == (True, False)
