"""Tests for the project bundle validator (ingest/validate_project.py).

The unit surface exercised here is ``validate_bundle`` against a temp bundle
built from the real ``data/_processed/soleil`` files, so the schemas are tested
against production-shaped data rather than synthetic fixtures. Error cases
inject a missing required field and a wrong-typed field into copies, proving
the reported JSON paths point at the exact offending field.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ingest.validate_project import validate_bundle

SOLEIL_SOURCE = Path(__file__).resolve().parent.parent / "data" / "_processed" / "soleil"
SIX_STEMS = [
    "project_info",
    "price_matrix",
    "unit_catalog",
    "payment_methods",
    "sales_contacts",
    "business_rules",
]


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    """Copy the real soleil bundle into a temp dir and return that dir."""
    for stem in SIX_STEMS:
        shutil.copy(SOLEIL_SOURCE / f"{stem}.json", tmp_path / f"{stem}.json")
    return tmp_path


def _load(data_dir: Path, stem: str):
    with (data_dir / f"{stem}.json").open(encoding="utf-8") as fh:
        return json.load(fh)


def _write(data_dir: Path, stem: str, data: dict):
    with (data_dir / f"{stem}.json").open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def test_all_six_valid_files_pass(bundle: Path):
    errors, loaded = validate_bundle("soleil", base=bundle)
    assert errors == []
    assert set(loaded) == set(SIX_STEMS)


def test_publish_gate_ok_when_contacts_nonempty(bundle: Path):
    sc = _load(bundle, "sales_contacts")
    sc["contacts"] = [{"name": "A", "role": "sales", "phone": "0901234567"}]
    _write(bundle, "sales_contacts", sc)
    errors, _ = validate_bundle("soleil", publish=True, base=bundle)
    assert errors == []


def test_publish_gate_rejects_empty_contacts(bundle: Path):
    errors, _ = validate_bundle("soleil", publish=True, base=bundle)
    assert any(
        stem == "sales_contacts"
        and path == "contacts"
        and "non-empty" in message
        for stem, path, message in errors
    )


def test_missing_required_field_reports_field_name(bundle: Path):
    pi = _load(bundle, "project_info")
    del pi["phap_ly"]["so_huu"]  # phap_ly.so_huu is required in the schema
    _write(bundle, "project_info", pi)
    errors, _ = validate_bundle("soleil", base=bundle)
    assert errors, "expected at least one validation error"
    assert any(
        stem == "project_info" and path == "phap_ly" and "so_huu" in message
        for stem, path, message in errors
    )


def test_wrong_type_reports_field_path(bundle: Path):
    uc = _load(bundle, "unit_catalog")
    # dien_tich_m2 must be a number; a string forces a type error at that key.
    uc["units"][0]["dien_tich_m2"] = "khong-phai-so"
    _write(bundle, "unit_catalog", uc)
    errors, _ = validate_bundle("soleil", base=bundle)
    assert errors, "expected at least one validation error"
    assert any(
        stem == "unit_catalog"
        and path == "units[0].dien_tich_m2"
        and "is not of type" in message
        for stem, path, message in errors
    )


def test_missing_file_reported(bundle: Path):
    (bundle / "price_matrix.json").unlink()
    errors, loaded = validate_bundle("soleil", base=bundle)
    assert any(
        stem == "price_matrix" and "missing file" in message
        for stem, path, message in errors
    )
    assert "price_matrix" not in loaded
