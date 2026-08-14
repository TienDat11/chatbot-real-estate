"""Unit tests for `ingest.camellia_docs` document registry builder (Story 2.2).

Every produced document must satisfy the required fields of docs/data-contract.md
(effective_from/effective_to aligned with db/schema.sql, fix M4), carry unique
doc_ids aligned with the existing seed (price-camellia-2026q3), and preserve the
GT-B6 corpus corrections inside the QnA text. No DB/network — pure builder.
"""

import datetime

from ingest.camellia_docs import (
    _CLEAN_PREFIXES,
    CAMPAIGN,
    REQUIRED_FIELDS,
    build_documents,
    validate_document,
)


def test_build_registry_has_all_expected_documents():
    docs = {d.doc_id for d in build_documents()}
    assert {
        "project-camellia-2026q3",
        "project-camellia-qna",
        "price-camellia-2026q3",
        "price-camellia-2026q3-payment",
        "price-camellia-2026q3-policy",
        "legal-gcnqsd-2011",
        "legal-qd254-2024",
        "legal-qd191-2025",
        "legal-cv12779-2026",
    } <= docs


def test_doc_ids_are_unique():
    ids = [d.doc_id for d in build_documents()]
    assert len(ids) == len(set(ids))


def test_seed_price_doc_id_preserved_for_campaign_fk():
    docs = {d.doc_id: d for d in build_documents()}
    assert docs["price-camellia-2026q3"].metadata["campaign"] == CAMPAIGN


def test_every_document_has_required_fields():
    for doc in build_documents():
        missing = validate_document(doc)
        assert not missing, f"{doc.doc_id} missing: {missing}"


def test_legal_effective_from_uses_issue_date_not_today():
    docs = {d.doc_id: d for d in build_documents()}
    assert docs["legal-gcnqsd-2011"].effective_from == datetime.date(2011, 10, 4)
    assert docs["legal-qd254-2024"].effective_from == datetime.date(2024, 1, 31)
    assert docs["legal-qd191-2025"].effective_from == datetime.date(2025, 1, 22)
    assert docs["legal-cv12779-2026"].effective_from == datetime.date(2026, 7, 13)


def test_price_and_project_open_interval_on_confirmation_date():
    docs = {d.doc_id: d for d in build_documents()}
    for doc_id in (
        "price-camellia-2026q3",
        "price-camellia-2026q3-payment",
        "price-camellia-2026q3-policy",
        "project-camellia-2026q3",
        "project-camellia-qna",
    ):
        doc = docs[doc_id]
        assert doc.effective_from == datetime.date(2026, 8, 13)
        assert doc.effective_to is None


def test_metadata_does_not_duplicate_top_level_columns():
    for doc in build_documents():
        for key in ("doc_id", "kind", "title", "content_hash", "status", "effective_from"):
            assert key not in doc.metadata, f"{doc.doc_id} metadata duplicates {key}"


def test_qna_applies_gtb6_corpus_fixes():
    text = {d.doc_id: d for d in build_documents()}["project-camellia-qna"].full_text
    assert "vay bù đắp khi mua" in text
    assert "vay bù đắp khu mua" not in text
    assert "MBV - dự kiến" in text
    assert "thời hạn CHỦ ĐẦU TƯ gửi hồ sơ" in text  # 50-ngày sổ đỏ nuance (GT-B6)


def test_qna_keeps_all_source_pages_including_subquestions():
    text = {d.doc_id: d for d in build_documents()}["project-camellia-qna"].full_text
    # Q13.2 (trang 16) — flagship product-structure answer must survive.
    assert "469 căn hộ" in text and "186 căn" in text and "2PN-2VS" in text


def test_legal_content_keeps_document_core_figures():
    docs = {d.doc_id: d for d in build_documents()}
    gcn = docs["legal-gcnqsd-2011"].full_text
    assert "CT09441" in gcn and "2297,0" in gcn
    assert "CT09442" in gcn and "2002,9" in gcn  # thửa 2 (page 4) not orphaned
    assert "Số lớn" not in gcn and "Sidebar" not in gcn  # sidebar/visual noise stripped
    cv = docs["legal-cv12779-2026"].full_text
    assert "469 căn hộ" in cv and "30.159,2" in cv and "Điều 24" in cv


def test_price_matrix_covers_four_payment_methods():
    text = {d.doc_id: d for d in build_documents()}["price-camellia-2026q3"].full_text
    for label in ("HTLS", "Thảnh thơi", "Thanh toán chuẩn", "Sớm 95%"):
        assert label in text
    assert "1.98 - 2.64 tỷ" in text  # Studio HTLS min-max spot check


def test_policy_doc_renders_thành_thơi_discount():
    text = {d.doc_id: d for d in build_documents()}["price-camellia-2026q3-policy"].full_text
    assert "Thảnh thơi" in text
    assert "5%" in text and "SÀN ĐẤT XANH" in text  # confirmed 5% tại sàn (đè ảnh 2%)


def test_required_fields_definition_covers_contract_exactly():
    assert REQUIRED_FIELDS == (
        "doc_id",
        "kind",
        "title",
        "source_file",
        "effective_from",
        "content_hash",
    )


def test_clean_prefix_filter_adds_sidebar_probe():
    assert "Sidebar" in _CLEAN_PREFIXES