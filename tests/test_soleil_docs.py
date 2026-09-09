"""Unit tests for `ingest.soleil_docs` document registry builder (Bug 6 fix).

Covers the 4-tower correction: the project overview must state the Tổ hợp Ánh
Dương - Soleil complex has 4 towers (A1, A2, B, D) with distances, while the
current sales catalog only covers A1 + D. Pure builder — no DB/network.
"""

import datetime

from ingest.soleil_docs import (
    CAMPAIGN,
    REQUIRED_FIELDS,
    build_documents,
    validate_document,
)


def test_build_registry_has_all_expected_documents():
    docs = {d.doc_id for d in build_documents()}
    assert {
        "project-soleil-2026q3",
        "project-soleil-qna",
        "price-soleil-2026q3",
        "price-soleil-2026q3-payment",
        "price-soleil-2026q3-policy",
        "rental-soleil-2026q3",
        "legal-soleil-chu-truong-2018",
        "legal-soleil-qd6608-2016",
        "legal-soleil-pccc-2017",
    } <= docs


def test_doc_ids_are_unique():
    ids = [d.doc_id for d in build_documents()]
    assert len(ids) == len(set(ids))


def test_seed_price_doc_id_preserved_for_campaign_fk():
    docs = {d.doc_id: d for d in build_documents()}
    assert docs["price-soleil-2026q3"].metadata["campaign"] == CAMPAIGN


def test_every_document_has_required_fields():
    for doc in build_documents():
        missing = validate_document(doc)
        assert not missing, f"{doc.doc_id} missing: {missing}"


def test_project_overview_states_four_towers():
    """Bug 6: the complex has 4 towers (A1, A2, B, D), not just the 2 sold now."""
    text = {d.doc_id: d for d in build_documents()}["project-soleil-2026q3"].full_text
    assert "4 tòa" in text
    assert "A1, A2, B, D" in text
    assert "A1-A2 40m" in text and "D-B 30m" in text
    # Current catalog covers only A1 + D; A2/B are not open for sale this wave.
    assert "Catalog bán hiện tại chỉ gồm Tòa A1 + Tòa D" in text


def test_rental_doc_renders_nightly_reference_rates():
    """rental-soleil-2026q3 must expose the VND/night reference rates."""
    text = {d.doc_id: d for d in build_documents()}["rental-soleil-2026q3"].full_text
    assert "VND/đêm" in text
    assert "2,800,000" in text  # Studio nightly reference (PA tự vận hành)


def test_required_fields_definition_covers_contract_exactly():
    assert REQUIRED_FIELDS == (
        "doc_id",
        "kind",
        "title",
        "source_file",
        "effective_from",
        "content_hash",
    )


def test_legal_docs_effective_from_uses_issue_date():
    docs = {d.doc_id: d for d in build_documents()}
    assert docs["legal-soleil-chu-truong-2018"].effective_from == datetime.date(2018, 9, 11)
    assert docs["legal-soleil-pccc-2017"].effective_from == datetime.date(2017, 8, 18)
