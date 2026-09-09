"""Wave F contracts: extraction accounting and evidence completeness."""

import json
from pathlib import Path

from ingest.load import INGEST_STATUSES
from ingest.run_camellia_ingest import plan_doc_ingest as plan_camellia
from ingest.run_soleil_ingest import plan_doc_ingest as plan_soleil

ROOT = Path(__file__).resolve().parents[1]


def test_status_contract_is_explicit():
    assert INGEST_STATUSES == {"extract_failed", "chunks_only", "facts_loaded", "review_required"}


def test_seed_carriers_are_convergence_safe():
    assert plan_camellia("price-camellia-2026q3") == (False, True)
    assert plan_soleil("price-soleil-2026q3") == (False, True)
    assert plan_soleil("rental-soleil-2026q3") == (False, True)


def test_evidence_manifest_has_source_and_location_for_every_type_and_method():
    data = json.loads(
        (ROOT / "data" / "_processed" / "evidence_manifest.json").read_text(encoding="utf-8")
    )
    for project in data["projects"].values():
        assert project["unit_types"]
        assert project["payment_methods"]
        for item in project["unit_types"] + project["payment_methods"]:
            assert item["source_file"]
            assert item.get("page") is not None
