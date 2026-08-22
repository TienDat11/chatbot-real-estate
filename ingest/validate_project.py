"""Validate a project's ``data/_processed/<project>/*.json`` bundle against the
shared JSON Schema standard (``ingest/schemas/*.schema.json``).

Why this module exists: the six processed files are the contract between the
ingest pipeline and every downstream consumer (facts engine, LightRAG, frontend
project switcher). A field renamed or typed differently in one project silently
breaks queries, so the bundle must be checked mechanically before publish.

Usage::

    python -m ingest.validate_project soleil            # schema check only
    python -m ingest.validate_project soleil --publish  # + publish gate (contacts)

Exit code 0 = valid, 1 = invalid. The directory is resolved as
``data/_processed/<project>/`` when present, else ``data/_processed/``
(the legacy Camellia layout keeps its files at the processed root).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError

# Each processed file maps 1:1 onto one schema document in ingest/schemas/.
SCHEMA_FILES: dict[str, str] = {
    "project_info": "project_info.schema.json",
    "price_matrix": "price_matrix.schema.json",
    "unit_catalog": "unit_catalog.schema.json",
    "payment_methods": "payment_methods.schema.json",
    "sales_contacts": "sales_contacts.schema.json",
    "business_rules": "business_rules.schema.json",
}

# Publish gate: a project cannot go live without a reachable sales desk.
# Empty contacts during extraction is normal; empty at publish is a defect.
PUBLISH_REQUIRES_NONEMPTY: dict[str, list[str]] = {
    "sales_contacts": ["contacts"],
}

# Field -> fact (EAV) mapping. Each entry documents how a key in the six
# processed files is loaded into the facts engine: the fact_subjects.subject_type
# (db/schema.sql), the project_key the fact is scoped to, and the attributes
# (fact_subjects.attrs) that further qualify the subject.
#
#   - unit        -> fact_subjects.subject_key 'unit:<project>/<ma_sp>'
#   - parcel      -> a land parcel subject
#   - project     -> fact_subjects.subject_key 'project:<project>'
#   - legal_fact  -> a legal/policy subject ('legal:<project>/<legal_ref>')
#   - taxon       -> a taxonomy term ('tax:<taxon_key>')
#
# The mapping is the contract the EAV loader (ingest/load.py) uses to build
# subjects and facts; a key absent here must not be silently dropped.
FIELD_TO_FACT: dict[str, dict[str, str]] = {
    # project_info -> project-level legal/scale facts
    "project_info.project": {"subject_type": "project", "attrs": "name"},
    "project_info.ten_phap_ly": {"subject_type": "project", "attrs": "legal_name"},
    "project_info.ten_thuong_mai": {"subject_type": "project", "attrs": "commercial_name"},
    "project_info.vi_tri": {"subject_type": "project", "attrs": "location"},
    "project_info.quy_mo": {"subject_type": "project", "attrs": "scale"},
    "project_info.phap_ly": {"subject_type": "legal_fact", "attrs": "legal_docs"},
    "project_info.ban_giao_noi_that": {"subject_type": "project", "attrs": "handover_standard"},
    "project_info.media": {"subject_type": "project", "attrs": "media"},
    # price_matrix -> per-unit price facts
    "price_matrix.types[].gia_m2_ty": {"subject_type": "unit", "attrs": "price_per_m2_ty"},
    "price_matrix.types[].gia": {"subject_type": "unit", "attrs": "price_vnd"},
    # unit_catalog -> per-unit catalog facts
    "unit_catalog.units[]": {"subject_type": "unit", "attrs": "unit"},
    "unit_catalog.units[].dien_tich_m2": {"subject_type": "unit", "attrs": "area_m2"},
    "unit_catalog.units[].loai": {"subject_type": "unit", "attrs": "type"},
    "unit_catalog.units[].huong": {"subject_type": "unit", "attrs": "direction"},
    # payment_methods -> policy facts per method
    "payment_methods.methods[]": {"subject_type": "taxon", "attrs": "payment_method"},
    "payment_methods.methods[].milestones[]": {"subject_type": "taxon", "attrs": "milestone"},
    # sales_contacts -> project-level contact roster
    "sales_contacts.chu_dau_tu": {"subject_type": "project", "attrs": "investor"},
    "sales_contacts.contacts[]": {"subject_type": "project", "attrs": "sales_contact"},
    # business_rules -> project-level policy facts
    "business_rules.rules.discount_matrix": {"subject_type": "project", "attrs": "discount_matrix"},
    "business_rules.rules.early_booking": {"subject_type": "project", "attrs": "early_booking"},
    "business_rules.rules.deposit": {"subject_type": "project", "attrs": "deposit"},
    "business_rules.rules.htls": {"subject_type": "project", "attrs": "htls"},
    "business_rules.rules.uy_thac_cho_thue": {"subject_type": "project", "attrs": "lease_entrust"},
    "business_rules.rules.vip": {"subject_type": "project", "attrs": "vip"},
    "business_rules.rules.kpbt": {"subject_type": "project", "attrs": "kpbt"},
    "business_rules.rules.thanh_toan_som": {"subject_type": "project", "attrs": "early_payment"},
    "business_rules.rules.sale_floor": {"subject_type": "project", "attrs": "sale_floor"},
    "business_rules.rules.price_quality": {"subject_type": "project", "attrs": "price_quality"},
    "business_rules.rules.contacts_update": {"subject_type": "project", "attrs": "contacts_update"},
}


def resolve_data_dir(project: str, base: Path | None = None) -> Path:
    """Return the processed-data dir for a project, root fallback for legacy layout."""
    base = base or Path(__file__).resolve().parent.parent / "data" / "_processed"
    sub = base / project
    return sub if sub.is_dir() else base


def load_schemas() -> dict[str, dict[str, Any]]:
    """Load every schema document under ingest/schemas/ keyed by file stem."""
    schema_dir = Path(__file__).resolve().parent / "schemas"
    out: dict[str, dict[str, Any]] = {}
    for stem, filename in SCHEMA_FILES.items():
        with (schema_dir / filename).open(encoding="utf-8") as fh:
            out[stem] = json.load(fh)
    return out


def validate_bundle(
    project: str,
    *,
    publish: bool = False,
    base: Path | None = None,
) -> tuple[list[tuple[str, str, str]], dict[str, dict[str, Any]]]:
    """Validate all six files of a project bundle.

    Returns ``(errors, loaded)`` where each error is ``(file_stem, json_path,
    message)`` with the JSON path rendered as dotted keys (e.g.
    ``units[0].dien_tich_m2``). ``loaded`` is the parsed bundle keyed by stem,
    empty when the file is missing or unparseable.
    """
    errors: list[tuple[str, str, str]] = []
    loaded: dict[str, dict[str, Any]] = {}
    schemas = load_schemas()
    data_dir = resolve_data_dir(project, base)

    for stem in SCHEMA_FILES:
        data_file = data_dir / f"{stem}.json"
        if not data_file.exists():
            errors.append((stem, "", f"missing file: {data_file.relative_to(base or data_dir)}"))
            continue
        try:
            with data_file.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            errors.append((stem, "", f"invalid JSON: {exc}"))
            continue
        loaded[stem] = data
        validator = Draft7Validator(schemas[stem])
        for err in sorted(validator.iter_errors(data), key=_error_key):
            errors.append((stem, _json_path(err), err.message))

        if publish and stem in PUBLISH_REQUIRES_NONEMPTY:
            for field in PUBLISH_REQUIRES_NONEMPTY[stem]:
                value = data.get(field)
                if not value:
                    errors.append(
                        (stem, field, f"publish gate: '{field}' must be non-empty")
                    )

    return errors, loaded


def _error_key(err: ValidationError) -> list[Any]:
    """Stable sort key so validation errors appear in document order."""
    return list(err.path)


def _json_path(err: ValidationError) -> str:
    """Render a jsonschema error path as dotted keys with array indexes."""
    parts: list[str] = []
    for seg in err.path:
        if isinstance(seg, int):
            parts.append(f"[{seg}]")
        elif not parts:
            parts.append(str(seg))
        else:
            last = parts[-1]
            parts[-1] = f"{last}.{seg}"
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m ingest.validate_project",
        description="Validate data/_processed/<project>/*.json against the shared schemas.",
    )
    parser.add_argument("project", help="project key, e.g. soleil")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="enforce publish gates (e.g. sales_contacts.contacts non-empty)",
    )
    args = parser.parse_args(argv)

    errors, _loaded = validate_bundle(args.project, publish=args.publish)
    if errors:
        for stem, path, message in errors:
            loc = f"{stem}.json" + (f" -> {path}" if path else "")
            print(f"FAIL  {loc}: {message}", file=sys.stderr)
        print(f"validation FAILED for '{args.project}' ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(f"validation PASSED for '{args.project}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
