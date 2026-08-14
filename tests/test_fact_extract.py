"""Unit tests for the ExtractedFact boundary model (ingest/fact_extract.py).

LLM extraction may omit `subject_type`; the model must derive it from the
subject_key namespace rather than dropping the whole fact (Story 2.3 live run bug).
"""

from ingest.fact_extract import ExtractedFact


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
