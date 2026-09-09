"""Round-2 defects D2 + D4 — value-aware fact notes and a humanized evidence legend.

The facts panel rendered `unit:camellia/2pn-goc | htls | interest_rate_pct: 0đ —
lãi suất %/năm (NULL = chưa có, không phải 0%)` for the HTLS policy whose rate is a
genuinely seeded 0.0000 subsidy, and FACT_EVIDENCE shipped raw machine keys to the
generation LLM with no legend. Notes must be value-aware, and every fe block must
carry additive ``*_display`` labels WITHOUT disturbing the machine keys that
guard_output grounds numbers (``fields``) and citations (``fe_id``) on.
"""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

from api.application.services.fact_display import (
    humanize_unit_subject_key,
    policy_display,
    subject_display,
)
from api.application.services.merge import build_evidence_context, build_facts
from api.application.services.sql_leg import _facts_note, build_fact_evidence
from api.domain.services.guard_output import evidence_values, guard_output

_AS_OF = date(2026, 9, 7)


def _fact_row(**overrides):
    row = {
        "fact_id": 1,
        "subject_key": "unit:camellia/2pn-goc",
        "display_name": "Căn hộ 2PN góc view núi Sơn Trà + biển",
        "fact_key": "interest_rate_pct",
        "policy_key": "htls",
        "value_num": Decimal("0.0000"),
        "value_text": None,
        "unit": "pct",
        "quality": "exact",
        "trust_level": "confirmed",
        "campaign_key": "camellia-2026q3",
        "source_doc_id": "price-camellia-2026q3",
    }
    row.update(overrides)
    return row


# --- D2: _facts_note is value-aware -----------------------------------------
def test_real_zero_interest_with_term_gets_duration_qualified_subsidy_note():
    note = _facts_note(_fact_row(), 18)
    assert "0%/năm" in note
    assert "18 tháng đầu" in note  # the HTLS subsidy is time-bounded, never unbounded
    assert "NULL" not in note
    assert "HTLS" in note  # names the owner-funded policy instead of calling it missing


def test_real_zero_interest_without_term_makes_no_unbounded_claim():
    note = _facts_note(_fact_row())
    assert "0%/năm" not in note  # no duration evidence -> the bare figure must not ship
    assert "NULL" not in note
    assert "thời hạn" in note


def test_zero_interest_without_a_known_policy_still_avoids_null_caveat():
    note = _facts_note(_fact_row(policy_key=None), 18)
    assert "0%/năm" in note
    assert "18 tháng đầu" in note
    assert "NULL" not in note
    assert "chính sách của chủ đầu tư" in note


def test_null_interest_keeps_the_null_caveat():
    note = _facts_note(_fact_row(value_num=None))
    assert "NULL = chưa có, không phải 0%" in note


def test_nonzero_rate_note_never_invents_a_figure():
    note = _facts_note(_fact_row(policy_key="bank_a", value_num=Decimal("8.5000")))
    assert "NULL" not in note
    assert "8.5" not in note and "8,5" not in note


def test_zero_deposit_pct_is_not_reported_as_missing():
    note = _facts_note(_fact_row(fact_key="deposit_pct", value_num=Decimal("0.00")))
    assert "NULL" not in note
    assert "%" in note


def test_term_months_real_value_carries_month_unit_and_no_null_caveat():
    note = _facts_note(_fact_row(fact_key="term_months", value_num=Decimal("18")))
    assert "NULL" not in note
    assert "tháng" in note


def test_term_months_null_gets_null_caveat():
    assert "NULL" in _facts_note(_fact_row(fact_key="term_months", value_num=None))


def test_range_and_approx_notes_keep_their_shape():
    note = _facts_note(_fact_row(quality="range", range_min=1, range_max=2))
    assert note.startswith("khoảng 1–2")
    assert "ước lượng" in _facts_note(_fact_row(quality="approx"))


def test_unknown_fact_key_falls_back_to_generic_note():
    assert _facts_note(_fact_row(fact_key="handover_quarter")) == "số liệu gốc từ dữ liệu cấu trúc"


# --- D4: display legend is additive ------------------------------------------
def test_policy_display_covers_every_seeded_payment_method():
    for key in ("htls", "chuan", "som95", "thanh_thoi", "thanhthoi", "bank_a", "support"):
        assert policy_display(key)
    assert policy_display("nope_not_a_policy") is None
    assert policy_display(None) is None


def test_humanize_unit_subject_key_maps_type_and_view_tokens():
    assert humanize_unit_subject_key("unit:camellia/2pn-mat-duong") == "Căn hộ 2PN mặt đường"
    assert humanize_unit_subject_key("unit:camellia/2pn-noi-khu") == "Căn hộ 2PN nội khu"
    assert humanize_unit_subject_key("unit:camellia/2pn-goc") == "Căn hộ 2PN góc"
    assert humanize_unit_subject_key("unit:camellia/studio") == "Căn hộ Studio"
    assert humanize_unit_subject_key("unit:camellia/1p1") == "Căn hộ 1PN+1"
    assert humanize_unit_subject_key("unit:camellia/3pn") == "Căn hộ 3PN"


def test_unresolvable_machine_key_yields_no_display_fallback():
    # Concrete unit codes must not be half-translated; the curated name wins.
    curated = "Căn CH-10 (2PN, 61.7 m², 2VS)"
    assert humanize_unit_subject_key("unit:camellia/CH-10") is None
    assert subject_display("unit:camellia/CH-10", curated) == curated
    assert subject_display("project:camellia") is None


def test_build_fact_evidence_adds_display_without_touching_machine_keys():
    rows = [
        _fact_row(),
        _fact_row(
            fact_id=2,
            subject_key="unit:camellia/2pn-mat-duong",
            display_name=None,
            fact_key="price_vnd",
            policy_key="som95",
            value_num=None,
            unit="vnd",
            quality="range",
            range_min=Decimal("3910000000"),
            range_max=Decimal("4670000000"),
        ),
        # Sibling duration fact for the SAME subject+policy as fe-001 — the
        # 0% note must pick it up and qualify the subsidy with the term.
        _fact_row(fact_id=3, fact_key="term_months", value_num=Decimal("18.0000")),
    ]
    fe = build_fact_evidence(rows, "facts", _AS_OF)

    assert fe[0]["subject"] == "unit:camellia/2pn-goc"
    assert fe[0]["policy_key"] == "htls"
    assert fe[0]["fe_id"] == "fe-001"
    assert fe[0]["fields"] == {"interest_rate_pct": 0}
    assert fe[0]["subject_display"] == "Căn hộ 2PN góc view núi Sơn Trà + biển"
    assert fe[0]["policy_display"] == "Phương án hỗ trợ lãi suất (HTLS)"
    assert "0%/năm" in fe[0]["note"] and "NULL" not in fe[0]["note"]
    assert "18 tháng đầu" in fe[0]["note"]

    assert fe[1]["subject_display"] == "Căn hộ 2PN mặt đường"
    assert fe[1]["policy_display"] == "Phương án thanh toán sớm 95%"


def test_build_fact_evidence_offer_branch_carries_display_too():
    rows = [
        {
            "subject_id": 7,
            "subject_key": "unit:camellia/2pn-goc",
            "display_name": "Căn hộ 2PN góc view núi Sơn Trà + biển",
            "policy_key": "htls",
            "price_vnd": Decimal("4630000000"),
            "interest_rate_pct": Decimal("0.0000"),
        }
    ]
    fe = build_fact_evidence(rows, "v_unit_offers", _AS_OF)
    assert fe[0]["subject_display"] == "Căn hộ 2PN góc view núi Sơn Trà + biển"
    assert fe[0]["policy_display"] == "Phương án hỗ trợ lãi suất (HTLS)"
    assert fe[0]["subject"] == "unit:camellia/2pn-goc"


def test_evidence_context_exposes_legend_and_backfills_other_producers():
    fe = build_fact_evidence([_fact_row()], "facts", _AS_OF)
    # The affordability leg builds entries outside build_fact_evidence.
    affordability_entry = {
        "fe_id": "fe-009",
        "subject": "Căn hộ 2PN View nội khu",
        "policy_key": "thanh_thoi",
        "fields": {"price_min_vnd": 3820000000},
    }
    ctx = build_evidence_context([*fe, affordability_entry])

    assert "subject_display" in ctx and "policy_display" in ctx
    assert "Căn hộ 2PN góc view núi Sơn Trà + biển" in ctx
    assert "Phương án hỗ trợ lãi suất (HTLS)" in ctx
    assert "unit:camellia/2pn-goc" in ctx  # machine key preserved, not replaced
    # Prose subjects are echoed so every entry offers the same legend field.
    assert "Phương án thanh toán thảnh thơi" in ctx


# --- D4 must not move guard_output ------------------------------------------
def test_guard_output_numeric_grounding_ignores_display_fields():
    facts = [
        {
            "fe_id": "fe-001",
            "subject": "unit:camellia/2pn-mat-duong",
            "subject_display": "Căn hộ 2PN mặt đường",
            "policy_key": "chuan",
            "policy_display": "Phương án thanh toán chuẩn",
            "fields": {"price_vnd": 4310000000},
            "note": "số tiền, đơn vị đồng (VND)",
        }
    ]
    answer = (
        "Phương án thanh toán chuẩn cho Căn hộ 2PN mặt đường: "
        "4.310.000.000 đồng [fe-001], là giá định hướng."
    )
    sources = [{"doc_id": "d1", "title": "Bảng giá"}]
    res = asyncio.run(guard_output(answer, facts, sources, {}, {"sql_row_count": 1}))
    assert res.verdicts["numeric_grounding"] == "pass"
    # Confidence is driven by numeric grounding + row counts, never by the added
    # display fields (citation_grounding is a separate, non-confidence verdict).
    assert res.confidence in ("HIGH", "MEDIUM")


def test_display_fields_do_not_reach_the_sse_facts_payload():
    # build_facts is the SSE `facts:` event contract; the legend is prompt-only so
    # the event schema stays byte-identical for the FE.
    fe = build_fact_evidence([_fact_row()], "facts", _AS_OF)
    assert sorted(build_facts(fe)[0]) == ["fe_id", "fields", "note", "policy_key", "subject"]


def test_display_text_never_becomes_a_groundable_number():
    # "95%" lives only in policy_display; grounding must still treat a quoted 95%
    # as orphan, proving the legend cannot launder ungrounded figures.
    facts = [
        {
            "fe_id": "fe-002",
            "subject": "unit:camellia/2pn-mat-duong",
            "policy_key": "som95",
            "policy_display": "Phương án thanh toán sớm 95%",
            "fields": {"price_vnd": 3910000000},
        }
    ]
    assert evidence_values(facts) == [Decimal("3910000000")]
    res = asyncio.run(
        guard_output(
            "Ưu đãi 95% áp cho Căn hộ 2PN mặt đường [fe-002], là giá định hướng.",
            facts,
            [],
            {},
            {"sql_row_count": 1},
        )
    )
    assert res.verdicts["numeric_grounding"] == "fail"


# --- D3: the prompt rules that stop the uniform-price collapse ---------------
def test_system_policy_gains_per_method_and_display_rules():
    p = (Path(__file__).resolve().parents[1] / "api" / "prompts" / "system_policy.md").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "TÁCH RIÊNG TỪNG PHƯƠNG ÁN THANH TOÁN",
        "nhiều ``policy_key``",
        "GỌI TÊN rõ phương thức",
        "Không lộ khóa nội bộ",
        "subject_display",
        "policy_display",
        '"0đ" cho lãi suất',
        "Không lặp dòng vô nghĩa",
    ):
        assert phrase in p, f"system_policy rule lost: {phrase}"
