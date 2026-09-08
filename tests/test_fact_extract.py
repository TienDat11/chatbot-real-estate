"""Unit tests for the ExtractedFact boundary model (ingest/fact_extract.py).

LLM extraction may omit `subject_type`; the model must derive it from the
subject_key namespace rather than dropping the whole fact (Story 2.3 live run bug).
"""

from decimal import Decimal
import pytest
from pydantic import ValidationError

from api.infrastructure.config.config import Settings
from ingest.fact_extract import ExtractedFact, _normalize_unit, _salvage_truncated_array
from ingest.run_soleil_ingest import main as run_soleil_main


def test_salvage_truncated_array_returns_completed_records():
    payload = (
        '[{"fact_key": "area_m2", "subject_key": "unit:camellia/studio", '
        '"unit": "m2"}, {"fact_key":'
    )
    assert _salvage_truncated_array(payload) == [
        {"fact_key": "area_m2", "subject_key": "unit:camellia/studio", "unit": "m2"}
    ]




def test_subject_type_derived_from_key_when_omitted():
    fact = ExtractedFact(
        fact_key="area_m2", subject_key="unit:camellia/studio", unit="m2", value_num=60
    )
    assert fact.subject_type == "unit"


def test_explicit_subject_type_wins_over_derivation():
    fact = ExtractedFact(
        fact_key="area_m2", subject_key="unit:camellia/studio",
        subject_type="project", unit="m2", value_num=60,
    )
    assert fact.subject_type == "project"


def test_unknown_prefix_falls_back_to_taxon():
    fact = ExtractedFact(fact_key="area_m2", subject_key="misc:unk", unit="m2", value_num=60)
    assert fact.subject_type == "taxon"


# ==============================================================================
# ExtractedFact.model_post_init quality normalization
# ==============================================================================


def test_quality_normalization_max_without_bounds_becomes_approx():
    fact = ExtractedFact(
        fact_key="area_m2",
        subject_key="unit:soleil/a1",
        unit="m2",
        quality="max",
        value_num=75,
    )
    assert fact.quality == "approx"


def test_quality_normalization_toi_thieu_becomes_approx():
    fact = ExtractedFact(
        fact_key="price_vnd",
        subject_key="unit:soleil/a1",
        unit="vnd",
        quality="tối thiểu",
        value_num=1000000000,
    )
    assert fact.quality == "approx"


def test_quality_normalization_range_like_with_bounds_becomes_range():
    fact = ExtractedFact(
        fact_key="area_m2",
        subject_key="unit:soleil/a1",
        unit="m2",
        quality="khoảng",
        range_min=Decimal("50"),
        range_max=Decimal("70"),
    )
    assert fact.quality == "range"


def test_quality_normalization_empty_or_whitespace_without_ranges_becomes_exact():
    # If quality is omitted, default is 'exact'
    fact_default = ExtractedFact(
        fact_key="area_m2",
        subject_key="unit:soleil/a1",
        unit="m2",
        value_num=60,
    )
    assert fact_default.quality == "exact"

    # If quality is empty/whitespace string, normalized becomes empty string and falls back to 'exact'
    fact_empty = ExtractedFact(
        fact_key="area_m2",
        subject_key="unit:soleil/a1",
        unit="m2",
        quality="   ",
        value_num=60,
    )
    assert fact_empty.quality == "exact"


def test_quality_normalization_valid_exact_stays_exact():
    fact = ExtractedFact(
        fact_key="area_m2",
        subject_key="unit:soleil/a1",
        unit="m2",
        quality="exact",
        value_num=60,
    )
    assert fact.quality == "exact"


# ==============================================================================
# _normalize_unit tests
# ==============================================================================


def test_normalize_unit_known_aliases():
    assert _normalize_unit("m²") == "m2"
    assert _normalize_unit("mét vuông") == "m2"
    assert _normalize_unit("đồng") == "vnd"
    assert _normalize_unit("%") == "pct"
    assert _normalize_unit("tháng") == "months"
    assert _normalize_unit("ngày") == "days"


def test_normalize_unit_unknown_unit_degrades_to_enum():
    assert _normalize_unit("unknown_unit_xyz") == "enum"
    assert _normalize_unit("can ho") == "enum"


# ==============================================================================
# FIX 1 and FIX 2 guards
# ==============================================================================


@pytest.mark.parametrize("bad_ws", ["", "   "])
def test_settings_empty_soleil_workspace_fails(bad_ws):
    with pytest.raises(ValidationError):
        Settings(app_env="dev", lightrag_workspace_soleil=bad_ws)


def test_settings_default_soleil_workspace_succeeds():
    s = Settings(app_env="dev")
    assert s.lightrag_workspace_soleil == "ragre_mvp"


def test_run_soleil_ingest_empty_only_exits_2(capsys):
    rc = run_soleil_main(["--only", ","])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error: --only must be a comma-separated list of non-empty doc_ids" in captured.err
