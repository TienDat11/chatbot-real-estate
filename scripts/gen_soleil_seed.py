"""Generate db/seed/soleil_campaign.sql from the processed Soleil corpus.

Reads the canonical processed JSONs under data/_processed/soleil and emits the
seed SQL that owns the unit/price/policy facts for the `soleil-2026q3` campaign.
This is the Soleil mirror of db/seed/camellia_rumor.sql: the seed rows carry
source_chunk_id NULL, so ingest/run_soleil_ingest.py preserves them through
load_document(preserve_seed_facts=True).

The generated file is committed; re-run this script to regenerate after the
corpus changes (idempotent output - ordering is deterministic).
"""

from __future__ import annotations

import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROC = _ROOT / "data" / "_processed" / "soleil"
_EXTRACT = _PROC / "_extract"
_OUT = _ROOT / "db" / "seed" / "soleil_campaign.sql"

CAMPAIGN = "soleil-2026q3"
PROJECT = "soleil"
DOC_ID = "price-soleil-2026q3"
EFFECTIVE_FROM = "2026-08-21"

# HTLS: max 65% loan -> 35% deposit (100 - vay_max_pct), mirroring Camellia's
# 30% deposit derived from its 70% loan cap (trust='estimate' for the deposit).
HTLS_DEPOSIT_PCT = 35.00


def _load(name: str) -> dict:
    return json.loads((_PROC / name).read_text(encoding="utf-8"))


def _q(value: object) -> str:
    """Quote a SQL string literal (single-quote doubling)."""
    return "'" + str(value).replace("'", "''") + "'"


def _to_float(value: float | int | str) -> float:
    """Coerce a numeric value that may be a comma-separated string (e.g. '5,144')."""
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    return float(value)


def _num(value: float | int | str) -> str:
    """Round a float to an integer VND amount (AD-14: NUMERIC(20,0))."""
    return str(int(round(_to_float(value))))


def _area(value: float | int | str) -> str:
    """Format an area value as NUMERIC(10,2)."""
    return f"{_to_float(value):.2f}"


def _attrs(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _tower_key(block: str) -> str:
    """Map a price-matrix block code to its business-rules tower key ('A1'/'D')."""
    return "A1" if block.upper() == "SLA1" else "D"


def _slug_loai(loai: str) -> str:
    return loai.lower()


def _normalize_ma_can(ma_can: str) -> str:
    """Map a price-sheet unit code to the catalog naming scheme.

    The price sheets prefix tower A1 with 'SLA1' while the catalog uses bare
    'A1' (tower D already shares the 'SLD' prefix in both sources).
    """
    if ma_can.startswith("SLA1-"):
        return "A1-" + ma_can[len("SLA1-"):]
    return ma_can


def _load_catalog() -> list[dict]:
    return _load("unit_catalog.json")["units"]


def _load_priced() -> list[dict]:
    """Flatten the three baskets (A1 / D / DXDH) into the 38 sample priced units."""
    data = json.loads((_EXTRACT / "priced_baskets.json").read_text(encoding="utf-8"))
    out: list[dict] = []
    for bucket in ("giỏ_hàng_chung_a1", "giỏ_hàng_chung_d", "dxdh"):
        out.extend(data.get(bucket, []))
    return out


def _load_types() -> list[dict]:
    return _load("price_matrix.json")["types"]


def _load_htls() -> dict:
    return _load("business_rules.json")["rules"]["htls"]


def _subject_catalog_key(ma_sp: str) -> str:
    return f"unit:{PROJECT}/{ma_sp}"


def _subject_type_key(block: str, loai: str) -> str:
    return f"unit:{PROJECT}/{_tower_key(block).lower()}-{_slug_loai(loai)}"


def _sql_catalog_subjects(units: list[dict]) -> list[str]:
    lines = [
        "INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)",
        "SELECT t.subject_key, 'unit', t.display_name, " + _q(PROJECT) + ", t.attrs::jsonb",
        "FROM (VALUES",
    ]
    for u in units:
        ma = u["ma_sp"]
        subject_key = _subject_catalog_key(ma)
        attrs = _attrs({
            "type": u.get("loai"),
            "floor": u.get("tang"),
            "orientation": u.get("huong"),
            "position": u.get("vi_tri"),
        })
        lines.append(f"  ({_q(subject_key)}, {_q('Căn ' + ma)}, {_q(attrs)}),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS t(subject_key, display_name, attrs)")
    lines.append("ON CONFLICT (subject_key) DO NOTHING;")
    return lines


def _sql_catalog_area_facts(units: list[dict]) -> list[str]:
    lines = [
        "INSERT INTO facts",
        "  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,",
        "   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)",
        "SELECT s.id, 'area_m2', NULL, " + _q(CAMPAIGN) + ", d.value_num::NUMERIC(10,2), 'm2', 'exact',",
        f"       FALSE, {_q(EFFECTIVE_FROM)}::date, NULL, {_q(DOC_ID)}, 0.99, 'confirmed'",
        "FROM (VALUES",
    ]
    for u in units:
        subject_key = _subject_catalog_key(u["ma_sp"])
        lines.append(f"  ({_q(subject_key)}, {_area(u['dien_tich_m2'])}::NUMERIC(10,2)),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS d(subject_key, value_num)")
    lines.append("JOIN fact_subjects s ON s.subject_key = d.subject_key")
    lines.append("WHERE NOT EXISTS (")
    lines.append("  SELECT 1 FROM facts f")
    lines.append("  WHERE f.subject_id = s.id AND f.fact_key = 'area_m2'")
    lines.append("    AND f.policy_key IS NULL AND f.effective_from = " + _q(EFFECTIVE_FROM) + "::date")
    lines.append(");")
    return lines


def _sql_priced_subjects(units: list[dict]) -> list[str]:
    lines = [
        "INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)",
        "SELECT t.subject_key, 'unit', t.display_name, " + _q(PROJECT) + ", t.attrs::jsonb",
        "FROM (VALUES",
    ]
    for u in units:
        ma = _normalize_ma_can(u["ma_can"])
        subject_key = _subject_catalog_key(ma)
        attrs = _attrs({
            "type": u.get("loai"),
            "floor": u.get("tang"),
            "sold_status": u.get("tinh_trang"),
        })
        lines.append(f"  ({_q(subject_key)}, {_q('Căn ' + ma)}, {_q(attrs)}),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS t(subject_key, display_name, attrs)")
    lines.append("ON CONFLICT (subject_key) DO NOTHING;")
    return lines


def _sql_priced_price_facts(units: list[dict]) -> list[str]:
    lines = [
        "INSERT INTO facts",
        "  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,",
        "   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)",
        "SELECT s.id, 'price_vnd', NULL, " + _q(CAMPAIGN) + ", d.value_num::NUMERIC(20,0), 'vnd', 'exact',",
        f"       FALSE, {_q(EFFECTIVE_FROM)}::date, NULL, {_q(DOC_ID)}, 0.80, 'estimate'",
        "FROM (VALUES",
    ]
    for u in units:
        subject_key = _subject_catalog_key(_normalize_ma_can(u["ma_can"]))
        price = _num(u["gia_gom_vat_chua_kpbt"])
        lines.append(f"  ({_q(subject_key)}, {price}::NUMERIC(20,0)),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS d(subject_key, value_num)")
    lines.append("JOIN fact_subjects s ON s.subject_key = d.subject_key")
    lines.append("WHERE NOT EXISTS (")
    lines.append("  SELECT 1 FROM facts f")
    lines.append("  WHERE f.subject_id = s.id AND f.fact_key = 'price_vnd'")
    lines.append("    AND f.policy_key IS NULL AND f.effective_from = " + _q(EFFECTIVE_FROM) + "::date")
    lines.append(");")
    return lines


def _sql_priced_area_facts(units: list[dict]) -> list[str]:
    # area_m2 for priced units; guarded so a catalog-seeded area is not duplicated.
    lines = [
        "INSERT INTO facts",
        "  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,",
        "   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)",
        "SELECT s.id, 'area_m2', NULL, " + _q(CAMPAIGN) + ", d.value_num::NUMERIC(10,2), 'm2', 'exact',",
        f"       FALSE, {_q(EFFECTIVE_FROM)}::date, NULL, {_q(DOC_ID)}, 0.80, 'confirmed'",
        "FROM (VALUES",
    ]
    for u in units:
        subject_key = _subject_catalog_key(_normalize_ma_can(u["ma_can"]))
        lines.append(f"  ({_q(subject_key)}, {_area(u['dien_tich_thong_thuy'])}::NUMERIC(10,2)),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS d(subject_key, value_num)")
    lines.append("JOIN fact_subjects s ON s.subject_key = d.subject_key")
    lines.append("WHERE NOT EXISTS (")
    lines.append("  SELECT 1 FROM facts f")
    lines.append("  WHERE f.subject_id = s.id AND f.fact_key = 'area_m2'")
    lines.append("    AND f.policy_key IS NULL AND f.effective_from = " + _q(EFFECTIVE_FROM) + "::date")
    lines.append(");")
    return lines


def _sql_type_subjects(types: list[dict]) -> list[str]:
    lines = [
        "INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)",
        "SELECT t.subject_key, 'unit', t.display_name, " + _q(PROJECT) + ", t.attrs::jsonb",
        "FROM (VALUES",
    ]
    for ty in types:
        subject_key = _subject_type_key(ty["block"], ty["loai"])
        display = f"Căn hộ {ty['loai']} {ty['ten_toa']}"
        attrs = _attrs({
            "type": ty["loai"],
            "block": ty["block"],
            "area_m2": ty["dien_tich_thong_thuy_m2"],
        })
        lines.append(f"  ({_q(subject_key)}, {_q(display)}, {_q(attrs)}),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS t(subject_key, display_name, attrs)")
    lines.append("ON CONFLICT (subject_key) DO NOTHING;")
    return lines


def _sql_type_range_facts(types: list[dict]) -> list[str]:
    # One 'chuan' guidance band per unit type (the event price already carries the
    # applied discount; the source has no per-method price grid). quality='range',
    # trust='estimate', mirroring Camellia's price_vnd range facts.
    lines = [
        "INSERT INTO facts",
        "  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,",
        "   range_min, range_max, volatile, effective_from, effective_to, source_doc_id,",
        "   extract_conf, trust_level)",
        "SELECT s.id, 'price_vnd', 'chuan', " + _q(CAMPAIGN) + ", NULL, 'vnd', 'range',",
        "       d.range_min::NUMERIC(20,0), d.range_max::NUMERIC(20,0),",
        f"       TRUE, {_q(EFFECTIVE_FROM)}::date, NULL, {_q(DOC_ID)}, 0.60, 'estimate'",
        "FROM (VALUES",
    ]
    for ty in types:
        subject_key = _subject_type_key(ty["block"], ty["loai"])
        lo, hi = ty["gia"]["tong_vnd"]
        lines.append(f"  ({_q(subject_key)}, {_num(lo)}::NUMERIC(20,0), {_num(hi)}::NUMERIC(20,0)),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS d(subject_key, range_min, range_max)")
    lines.append("JOIN fact_subjects s ON s.subject_key = d.subject_key")
    lines.append("WHERE NOT EXISTS (")
    lines.append("  SELECT 1 FROM facts f")
    lines.append("  WHERE f.subject_id = s.id AND f.fact_key = 'price_vnd'")
    lines.append("    AND COALESCE(f.policy_key, '') = 'chuan'")
    lines.append("    AND f.effective_from = " + _q(EFFECTIVE_FROM) + "::date")
    lines.append(");")
    return lines


def _sql_htls_facts(types: list[dict], htls: dict) -> list[str]:
    # HTLS loan policy for all 9 unit types (policy_key='htls'). Deposit 35% is
    # derived from the 65% loan cap (trust='estimate'); term 18 (A1) / 12 (D) and
    # 0% interest are confirmed by the sale policy. interest 0.0000 = true 0%.
    lines = [
        "INSERT INTO facts",
        "  (subject_id, fact_key, policy_key, campaign_key, value_num, unit, quality,",
        "   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)",
        "SELECT s.id, d.fact_key, 'htls', " + _q(CAMPAIGN) + ", d.value_num, d.unit, 'exact',",
        f"       FALSE, {_q(EFFECTIVE_FROM)}::date, NULL, {_q(DOC_ID)}, 0.90,",
        "       CASE WHEN d.fact_key = 'deposit_pct' THEN 'estimate' ELSE 'confirmed' END",
        "FROM fact_subjects s",
        "JOIN (VALUES",
    ]
    for ty in types:
        subject_key = _subject_type_key(ty["block"], ty["loai"])
        term = htls[_tower_key(ty["block"])]["term_months"]
        lines.append(f"  ({_q(subject_key)}, 'deposit_pct', {HTLS_DEPOSIT_PCT:.2f}::NUMERIC(5,2), 'pct'),")
        lines.append(f"  ({_q(subject_key)}, 'term_months', {term}::NUMERIC, 'months'),")
        lines.append(f"  ({_q(subject_key)}, 'interest_rate_pct', 0.0000::NUMERIC(6,4), 'pct'),")
    lines[-1] = lines[-1].rstrip(",")
    lines.append(") AS d(subject_key, fact_key, value_num, unit)")
    lines.append("ON s.subject_key = d.subject_key")
    lines.append("WHERE NOT EXISTS (")
    lines.append("  SELECT 1 FROM facts f")
    lines.append("  WHERE f.subject_id = s.id AND f.fact_key = d.fact_key")
    lines.append("    AND COALESCE(f.policy_key, '') = 'htls'")
    lines.append("    AND f.effective_from = " + _q(EFFECTIVE_FROM) + "::date")
    lines.append(");")
    return lines


def _sql_project_subject() -> list[str]:
    attrs = _attrs({
        "developer": "Công ty Cổ phần PPC An Thịnh Đà Nẵng",
        "location": "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
        "total_units": 1087,
        "handover": "Tòa D: 07/2025; Tòa A1: Q4/2027",
        "trust": "estimate",
    })
    return [
        "INSERT INTO fact_subjects (subject_key, subject_type, display_name, project_key, attrs)",
        "VALUES",
        f"  ({_q('project:soleil')}, 'project', {_q('The Soleil Đà Nẵng')}, {_q(PROJECT)}, {_q(attrs)}::jsonb)",
        "ON CONFLICT (subject_key) DO NOTHING;",
    ]


def _sql_htls_banks() -> list[str]:
    return [
        "INSERT INTO facts",
        "  (subject_id, fact_key, policy_key, campaign_key, value_text, unit, quality,",
        "   volatile, effective_from, effective_to, source_doc_id, extract_conf, trust_level)",
        "SELECT s.id, 'htls_banks', 'htls', " + _q(CAMPAIGN) + ", v.value_text, 'enum', v.quality,",
        f"       FALSE, {_q(EFFECTIVE_FROM)}::date, NULL, {_q(DOC_ID)}, 0.90, v.trust",
        "FROM fact_subjects s",
        "CROSS JOIN (VALUES",
        "  ('Vietinbank - CN Đà Nẵng (ngân hàng chỉ định của CĐT)', 'exact', 'confirmed')",
        ") AS v(value_text, quality, trust)",
        "WHERE s.subject_key = 'project:soleil'",
        "AND NOT EXISTS (",
        "  SELECT 1 FROM facts f",
        "  WHERE f.subject_id = s.id AND f.fact_key = 'htls_banks'",
        "    AND f.value_text = v.value_text",
        ");",
    ]


def build_sql() -> str:
    catalog = _load_catalog()
    priced = _load_priced()
    types = _load_types()
    htls = _load_htls()

    header = [
        "-- rag-real-estate - Seed Soleil Q3/2026 campaign (idempotent, UTF-8).",
        "-- Run after db/schema.sql + db/camellia_estimate.sql (adds facts.trust_level).",
        "--",
        "-- Soleil mirror of db/seed/camellia_rumor.sql: the seed rows carry",
        "-- source_chunk_id NULL, so ingest/run_soleil_ingest.py preserves them via",
        "-- load_document(preserve_seed_facts=True).",
        "--",
        "-- Contents: price document + soleil-2026q3 campaign; 1087 catalog unit",
        "-- subjects with area_m2 (confirmed); 38 event-basket priced units with",
        "-- price_vnd (estimate) + area_m2; 9 unit-type subjects with a 'chuan'",
        "-- range price fact + HTLS loan policy (deposit 35% / term 18 A1, 12 D /",
        "-- 0% interest); the project subject + HTLS bank.",
        "--",
        "-- Generated by scripts/gen_soleil_seed.py from data/_processed/soleil/*.json.",
        "",
        "BEGIN;",
        "",
    ]

    doc = [
        "INSERT INTO documents",
        "  (doc_id, kind, title, source_file, effective_from, effective_to, status, content_hash, metadata)",
        "VALUES",
        f"  ({_q(DOC_ID)}, 'price', {_q('Bảng giá sự kiện The Soleil Đà Nẵng Q3/2026 (event basket)')},",
        f"   {_q('data/_processed/soleil/price_matrix.json')}, {_q(EFFECTIVE_FROM)}, NULL, 'published',",
        f"   encode(digest('seed:{DOC_ID}', 'sha256'), 'hex'),",
        f"   {_q('{\"project\":\"soleil\",\"campaign\":\"soleil-2026q3\",\"currency\":\"VND\",\"trust\":\"estimate\"}')})",
        "ON CONFLICT (doc_id) DO NOTHING;",
        "",
    ]

    campaign = [
        "INSERT INTO campaigns",
        "  (campaign_key, project_key, effective_from, effective_to, source_doc_id, status)",
        "VALUES",
        f"  ({_q(CAMPAIGN)}, {_q(PROJECT)}, {_q(EFFECTIVE_FROM)}, NULL, {_q(DOC_ID)}, 'active')",
        "ON CONFLICT (campaign_key) DO NOTHING;",
        "",
    ]

    body: list[str] = []
    body.append("-- Catalog units (1087) + area_m2 facts (quality=exact, trust=confirmed).")
    body.extend(_sql_catalog_subjects(catalog))
    body.append("")
    body.extend(_sql_catalog_area_facts(catalog))
    body.append("")
    body.append("-- 38 event-basket priced units + price_vnd/area_m2 facts (price=estimate).")
    body.extend(_sql_priced_subjects(priced))
    body.append("")
    body.extend(_sql_priced_price_facts(priced))
    body.append("")
    body.extend(_sql_priced_area_facts(priced))
    body.append("")
    body.append("-- 9 unit-type subjects + a 'chuan' range price fact (quality=range, trust=estimate).")
    body.extend(_sql_type_subjects(types))
    body.append("")
    body.extend(_sql_type_range_facts(types))
    body.append("")
    body.append("-- HTLS loan policy (deposit_pct/term_months/interest_rate_pct) per unit type.")
    body.extend(_sql_htls_facts(types, htls))
    body.append("")
    body.append("-- Project subject + HTLS bank.")
    body.extend(_sql_project_subject())
    body.append("")
    body.extend(_sql_htls_banks())
    body.append("")

    return "\n".join(header + doc + campaign + body + ["COMMIT;", ""])


def main() -> int:
    sql = build_sql()
    _OUT.write_text(sql, encoding="utf-8")
    print(f"wrote {_OUT} ({len(sql)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
