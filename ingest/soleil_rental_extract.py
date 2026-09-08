"""Deterministic extractor: Soleil estimated rental cash-flow projections.

Reads the two "Bảng tính dòng tiền thuê ước tính" workbooks under data/soleil
and writes a normalized projection model to
data/_processed/soleil/rental_projections.json. That file feeds the registry
doc ``rental-soleil-2026q3`` (ingest/soleil_docs.py), so chatbot answers about
buy-to-rent / leaseback economics (nightly reference rent, occupancy, expected
revenue/cost/profit and owner yield) cite real figures from the raw sheets.

Why deterministic: the investment economics live in XLSX cells, not scanned
PDFs, so parsing cells is more reliable than OCR and never guesses. Re-running
overwrites the JSON identically (idempotent).

Sheet layout notes (verified against both workbooks):
  * First-year sheet "Bảng tính năm đầu": label column is column 1 (col0 empty).
      - row: col1='Căn hộ' col2=ma col3=loai col4='Giá thuê tham khảo' col5=nightly
      - row: col1='Diện tích' col2=m2 col4='Tỷ lệ lấp đầy tham khảo' col5=occupancy
      - row: col1='Giá Tham khảo' col2=price col4='Doanh Thu dự kiến' col5=revenue
      - self-op: 'Chi phí dự kiến' col4=cost, 'Lợi nhuận dự kiến' col4=profit,
        'Tỷ lệ Lợi nhuận/Vốn bỏ ra/năm' col7=yield-ratio
      - entrust: 'Chia sẻ doanh thu CĐT' col2=ratio col3=vnd,
        'Chia sẻ doanh thu KH' col2=ratio col3=vnd
  * Annual sheet (self-op 'Dòng tiền hàng năm'; entrust 'Studio'/'1BR'/'2BR'):
    header row col2='Diễn giải', cols 3..12 = years, col13='TOTAL'.
    Data rows: col0=row label ('1','2',...), col1=description, values cols 3..12,
    TOTAL col13 (present on the yield row).
"""

from __future__ import annotations

import json
import pathlib

import openpyxl

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "soleil"
OUT = ROOT / "data" / "_processed" / "soleil"

SELF_OP_XLSX = "2025.10.02 - Bảng tính dòng tiền thuê ước tính (PA tự vận hành).xlsx"
ENTRUST_XLSX = "2025.11.14 - Bảng tính dòng tiền thuê ước tính (PA ủy thác).xlsx"

# Value columns for the 10-year projection sheets.
_ANNUAL_VAL_START = 3
_ANNUAL_VAL_END = 13
_TOTAL_COL = 13


def _rows(path: pathlib.Path, sheet: str) -> list[tuple]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    return rows


def _f(v):
    """Normalize a cell to a float when numeric, else stripped string, else None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    return s if s else None


def _find_fy_row(rows: list[tuple], needle: str, start: int = 0) -> int:
    """First row whose column-1 text starts with needle (first-year sheets)."""
    for i in range(start, len(rows)):
        c1 = _f(rows[i][1]) if len(rows[i]) > 1 else None
        if c1 and str(c1).startswith(needle):
            return i
    raise ValueError(f"row not found: {needle}")


def _fy(rows: list[tuple], needle: str, col: int):
    """Cell value at col on the first row whose col1 starts with needle."""
    return _f(rows[_find_fy_row(rows, needle)][col])


def _round_vnd(x) -> int:
    return int(round(float(x)))


def _round_pct(x) -> float:
    """Ratio (0..1) -> percentage number rounded to 2 dp."""
    return round(float(x) * 100.0, 2)


def _parse_annual(rows: list[tuple]) -> dict:
    """Parse a 10-year projection sheet body into {years, rows{label: {...}}}."""
    header = None
    for i, row in enumerate(rows):
        cells = [_f(v) for v in row]
        if any(c == "Diễn giải" for c in cells):
            header = i
            break
    if header is None:
        raise ValueError("annual header 'Diễn giải' not found")
    years = [int(_f(v)) for v in rows[header][_ANNUAL_VAL_START:_ANNUAL_VAL_END]]
    out: dict[str, dict] = {}
    for row in rows[header + 1 :]:
        c0, c1 = _f(row[0]), _f(row[1])
        if c1 is None or not str(c1).strip():
            continue
        values = [_f(v) for v in row[_ANNUAL_VAL_START:_ANNUAL_VAL_END]]
        total = _f(row[_TOTAL_COL]) if len(row) > _TOTAL_COL else None
        out[str(c0)] = {"label": str(c1), "values": values, "total": total}
    return {"years": years, "rows": out}


def _unit_identity(rows: list[tuple]) -> dict:
    """Extract unit identity cells from the top of an annual projection sheet."""
    ma = loai = dt = price = None
    for row in rows[:8]:
        c1 = _f(row[1]) if len(row) > 1 else None
        if not c1:
            continue
        if "Căn hộ" in str(c1):
            ma, loai = _f(row[2]), _f(row[3])
        elif "Diện tích" in str(c1):
            dt = _f(row[2])
        elif "Giá Tham khảo" in str(c1):
            price = _f(row[2])
    return {"ma_can": ma, "loai": loai, "dien_tich_m2": dt, "gia_tham_khao_vnd": price}


def _find_label_row(parsed: dict, needle: str) -> dict | None:
    for payload in parsed["rows"].values():
        if needle in str(payload["label"]):
            return payload
    return None


def _annual_units(rows: list[tuple]) -> list[dict]:
    """Each annual projection sheet yields exactly one unit projection dict."""
    ident = _unit_identity(rows)
    parsed = _parse_annual(rows)
    years = parsed["years"]

    gia_phong = _find_label_row(parsed, "Giá phòng")
    lap_day = _find_label_row(parsed, "lấp đầy")
    doanh_thu = _find_label_row(parsed, "Doanh Thu Dự kiến")
    kh_share = _find_label_row(parsed, "Doanh Thu trước chi phí của KH")
    chi_phi = _find_label_row(parsed, "Chi phí dự kiến") or _find_label_row(parsed, "Chi phí")
    loi_nhuan = _find_label_row(parsed, "Lợi nhuận dự kiến")
    thu_nhap = _find_label_row(parsed, "Thu nhập sau chi phí")
    yield_row = _find_label_row(parsed, "Tỷ suất lợi nhuận của Chủ Sở Hữu")

    cash_flow = []
    for i, year in enumerate(years):

        def _v(row: dict | None, index: int = i) -> float | None:
            if row is None:
                return None
            return row["values"][index] if index < len(row["values"]) else None

        entry = {"year": year}
        if gia_phong:
            entry["gia_phong_vnd_ngay"] = _round_vnd(_v(gia_phong))
        if lap_day:
            entry["ty_le_lap_day"] = _v(lap_day)
        if doanh_thu:
            entry["doanh_thu_vnd"] = _round_vnd(_v(doanh_thu))
        if kh_share:
            entry["share_kh_vnd"] = _round_vnd(_v(kh_share))
        if chi_phi:
            entry["chi_phi_vnd"] = _round_vnd(_v(chi_phi))
        if loi_nhuan:
            entry["loi_nhuan_vnd"] = _round_vnd(_v(loi_nhuan))
        if thu_nhap:
            entry["thu_nhap_sau_chi_phi_vnd"] = _round_vnd(_v(thu_nhap))
        if yield_row:
            y = _v(yield_row)
            if y is not None:
                entry["ty_suat_loi_nhuan_von_pct"] = _round_pct(y)
        cash_flow.append(entry)

    return [
        {
            "ma_can": ident["ma_can"],
            "loai": ident["loai"],
            "dien_tich_m2": round(float(ident["dien_tich_m2"]), 2)
            if ident["dien_tich_m2"] is not None
            else None,
            "gia_tham_khao_vnd": _round_vnd(ident["gia_tham_khao_vnd"])
            if ident["gia_tham_khao_vnd"] is not None
            else None,
            "cash_flow_10y": cash_flow,
            "cumulative_10y_yield_pct": (
                _round_pct(yield_row["total"])
                if yield_row and yield_row["total"] is not None
                else None
            ),
        }
    ]


def extract_self_operated() -> dict:
    """Tower A1 scenario (PA tự vận hành): first-year calc + 10-year cash flow."""
    path = DATA / SELF_OP_XLSX
    first_rows = _rows(path, "Bảng tính năm đầu")

    gia_thue = _fy(first_rows, "Căn hộ", 5)
    lap_day = _fy(first_rows, "Diện tích", 5)
    doanh_thu = _fy(first_rows, "Giá Tham khảo", 5)
    chi_phi = _fy(first_rows, "Chi phí dự kiến", 4)
    loi_nhuan = _fy(first_rows, "Lợi nhuận dự kiến", 4)
    # Yield-on-capital ratio sits on the same row as 'Chi phí dự kiến' (col 7).
    try:
        ty_suat = _fy(first_rows, "Chi phí dự kiến", 7)
    except ValueError:
        ty_suat = None

    def cost_detail(needle: str) -> float | None:
        """Cost-breakdown cells live in col 6/7 of the first-year sheet."""
        for row in first_rows:
            c6 = _f(row[6]) if len(row) > 6 else None
            if c6 and needle in str(c6):
                return _f(row[7]) if len(row) > 7 else None
        return None

    detail = {
        "nhan_su_vnd_thang": cost_detail("Nhân sự"),
        "phi_qlv_vnd_m2_thang": cost_detail("QLDV"),
        "phi_van_hanh_pct": cost_detail("vận hành"),
        "phi_mkt_pct": cost_detail("MKT"),
        "phi_khau_hao_bao_tri_pct": cost_detail("khấu hao"),
    }

    annual = _annual_units(_rows(path, "Dòng tiền hàng năm"))[0]

    return {
        "id": "tu_van_hanh",
        "label": "PA tự vận hành (chủ sở hữu tự khai thác cho thuê)",
        "tower": "A1",
        "first_year": annual["cash_flow_10y"][0]["year"] if annual["cash_flow_10y"] else None,
        "unit_types": [
            {
                "loai": annual["loai"],
                "ma_can": annual["ma_can"],
                "dien_tich_m2": annual["dien_tich_m2"],
                "gia_tham_khao_vnd": annual["gia_tham_khao_vnd"],
                "gia_thue_tham_khao_vnd_ngay": _round_vnd(gia_thue) if gia_thue else None,
                "ty_le_lap_day_tham_khao": lap_day,
                "doanh_thu_nam_dau_vnd": _round_vnd(doanh_thu) if doanh_thu else None,
                "chi_phi_nam_dau_vnd": _round_vnd(chi_phi) if chi_phi else None,
                "loi_nhuan_nam_dau_vnd": _round_vnd(loi_nhuan) if loi_nhuan else None,
                "ty_suat_loi_nhuan_von_nam_dau_pct": _round_pct(ty_suat) if ty_suat else None,
                "cost_detail": detail,
                "cash_flow_10y": annual["cash_flow_10y"],
                "cumulative_10y_yield_pct": annual["cumulative_10y_yield_pct"],
            }
        ],
    }


def extract_entrusted() -> dict:
    """Tower D scenario (PA ủy thác): 3 unit-type first-year calcs + 10-year flows."""
    path = DATA / ENTRUST_XLSX
    first_rows = _rows(path, "Bảng tính năm đầu")

    blocks: list[list[tuple]] = []
    cur: list[tuple] = []
    for row in first_rows:
        c1 = _f(row[1]) if len(row) > 1 else None
        if c1 and str(c1).startswith("Căn hộ") and cur:
            blocks.append(cur)
            cur = [row]
        else:
            cur.append(row)
    if cur:
        blocks.append(cur)

    unit_types = []
    for block in blocks:
        try:
            gia_thue = _fy(block, "Căn hộ", 5)
        except ValueError:
            continue  # leading rows before the first unit block
        lap_day = _fy(block, "Diện tích", 5)
        doanh_thu = _fy(block, "Giá Tham khảo", 5)
        share_cdt = _fy(block, "Chia sẻ doanh thu CĐT", 3)
        share_kh = _fy(block, "Chia sẻ doanh thu KH", 3)
        ma_can = _fy(block, "Căn hộ", 2)
        loai = _fy(block, "Căn hộ", 3)
        dien_tich = _fy(block, "Diện tích", 2)
        gia = _fy(block, "Giá Tham khảo", 2)

        sheet_name = {"Stu": "Studio", "1BR": "1BR", "2BR": "2BR"}.get(str(loai))
        annual = _annual_units(_rows(path, sheet_name))[0] if sheet_name else {}

        net_year1 = (
            annual["cash_flow_10y"][0]["thu_nhap_sau_chi_phi_vnd"]
            if annual.get("cash_flow_10y")
            else None
        )
        ty_suat = round(net_year1 / gia * 100.0, 2) if net_year1 and gia else None

        unit_types.append(
            {
                "loai": loai,
                "ma_can": ma_can,
                "dien_tich_m2": round(float(dien_tich), 2) if dien_tich is not None else None,
                "gia_tham_khao_vnd": _round_vnd(gia) if gia else None,
                "gia_thue_tham_khao_vnd_ngay": _round_vnd(gia_thue) if gia_thue else None,
                "ty_le_lap_day_tham_khao": lap_day,
                "doanh_thu_nam_dau_vnd": _round_vnd(doanh_thu) if doanh_thu else None,
                "share_cdt_nam_dau_vnd": _round_vnd(share_cdt) if share_cdt else None,
                "share_kh_nam_dau_vnd": _round_vnd(share_kh) if share_kh else None,
                "thu_nhap_sau_chi_phi_nam_dau_vnd": net_year1,
                "ty_suat_loi_nhuan_von_nam_dau_pct": ty_suat,
                "phi_qlv_vnd_m2_thang": 38500,
                "mien_phi_qlv_nam_dau": 2,
                "phi_du_phong_pct": 5.0,
                "cash_flow_10y": annual.get("cash_flow_10y", []),
                "cumulative_10y_yield_pct": annual.get("cumulative_10y_yield_pct"),
            }
        )

    return {
        "id": "uy_thac",
        "label": "PA ủy thác (giao đơn vị vận hành khai thác, chia sẻ doanh thu)",
        "tower": "D",
        "revenue_share": {"cdt": 0.6, "kh": 0.4},
        "first_year": unit_types[0]["cash_flow_10y"][0]["year"]
        if unit_types and unit_types[0]["cash_flow_10y"]
        else None,
        "unit_types": unit_types,
    }


def build_rental_projections() -> dict:
    """Assemble the canonical rental_projections.json document."""
    return {
        "project": "The Soleil Đà Nẵng",
        "source": f"{SELF_OP_XLSX} (tòa A1) + {ENTRUST_XLSX} (tòa D)",
        "note": (
            "Bảng tính dòng tiền thuê ước tính chỉ mang tính tham khảo, ước lượng; "
            "giá trị thay đổi dựa trên khảo sát thực tế và thông tin thị trường. "
            "Không phải cam kết lợi nhuận cho thuê."
        ),
        "scenarios": [
            extract_self_operated(),
            extract_entrusted(),
        ],
    }


def main() -> int:
    data = build_rental_projections()
    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / "rental_projections.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written ->", out_path)
    for scenario in data["scenarios"]:
        print(f"- {scenario['id']} (tòa {scenario['tower']}, từ {scenario['first_year']}):")
        for ut in scenario["unit_types"]:
            yield_pct = ut.get("ty_suat_loi_nhuan_von_nam_dau_pct")
            print(
                f"    {ut['loai']} {ut['ma_can']} {ut['dien_tich_m2']} m2 "
                f"giá {ut['gia_tham_khao_vnd']:,} | thuê {ut['gia_thue_tham_khao_vnd_ngay']:,}/đêm "
                f"lấp đầy {ut['ty_le_lap_day_tham_khao']} | suất NN {yield_pct}%"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
