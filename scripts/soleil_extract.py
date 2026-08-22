"""Deterministic XLSX extractor for the Soleil corpus (Story: new-project Soleil).

Reads the Soleil spreadsheets under data/soleil and writes structured JSON into
data/_processed/soleil/_extract/. This stage does NOT touch the registry or the
embedding pipeline — it only normalizes the spreadsheet cells into the canonical
field vocabulary so the ingest builder (ingest/soleil_docs.py) can render text.

Why deterministic: the price / unit / payment facts live in XLSX cells, not in
scanned PDFs, so parsing cells is more reliable than OCR and never guesses.
"""

from __future__ import annotations

import json
import os
import pathlib
from collections import Counter, defaultdict

import openpyxl

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "soleil"
OUT = ROOT / "data" / "_processed" / "soleil" / "_extract"
OUT.mkdir(parents=True, exist_ok=True)


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


def extract_product_list() -> list[dict]:
    """Full unit catalog: block A1 (self-operated) + block D (entrusted)."""
    sources = [
        DATA / "2025.10.02 - Bảng tính dòng tiền thuê ước tính (PA tự vận hành).xlsx",
        DATA / "2025.11.14 - Bảng tính dòng tiền thuê ước tính (PA ủy thác).xlsx",
    ]
    units = []
    for path in sources:
        rows = _rows(path, "Danh sách sản phẩm")
        for r in rows[5:]:  # header at row index 4
            code = _f(r[4])  # Product code column
            if not code:
                continue
            units.append({
                "stt": _f(r[0]),
                "block": _f(r[1]),
                "tang": _f(r[2]),
                "so_can": _f(r[3]),
                "ma_sp": code,
                "loai": _f(r[5]),
                "dien_tich_m2": _f(r[6]),
                "vi_tri": _f(r[7]),
                "huong": _f(r[8]),
            })
    return units


def extract_priced_baskets() -> dict:
    """Event baskets (common cart + ĐXDH) that carry per-unit prices + status."""
    out = {}

    def parse(path: pathlib.Path, sheet: str):
        rows = _rows(path, sheet)
        units = []
        for r in rows[5:]:
            code = _f(r[4])
            if not code:
                continue
            units.append({
                "stt": _f(r[0]), "toa": _f(r[1]), "tang": _f(r[2]),
                "so_can": _f(r[3]), "ma_can": code, "dien_tich_thong_thuy": _f(r[5]),
                "loai": _f(r[6]), "gia_chua_vat_kpbt": _f(r[7]),
                "gia_gom_vat_chua_kpbt": _f(r[8]), "tinh_trang": _f(r[9]),
            })
        return units

    chung = DATA / "2025.12.28-Giỏ Hàng Chung -DA The Soleil DN - Drive.xlsx"
    out["giỏ_hàng_chung_a1"] = parse(chung, "Gió hàng chung A1")
    out["giỏ_hàng_chung_d"] = parse(chung, "Gió hàng chung D")

    dxdh = DATA / "2025.12.21-Giỏ Hàng ĐXDH -DA The Soleil DN.xlsx"
    out["dxdh"] = parse(dxdh, "ĐXDH")
    return out


def extract_ptg() -> dict:
    """Payment-method discounts + per-method quotation anchors from both PTG files."""
    result = {}
    specs = [
        ("A1", DATA / "2026.03.03 - PTG A1 dự án Soleil ĐN (Gửi ĐL).xlsx",
         ["TTTĐ", "TTĐB", "TTS 95%", "TTS 70%", "TTS 50%", "HTLS"]),
        ("D", DATA / "2026.05.08 - PTG D dự án Soleil ĐN (Gửi ĐL).xlsx",
         ["TTTĐ", "TTS 95%", "TTS 95% (DTS)", "TTS 70%", "TTS 70% (NNS)", "HTLS"]),
    ]
    for block, path, sheets in specs:
        result[block] = {}
        for sn in sheets:
            rows = _rows(path, sn)
            discounts = {}
            # Extract discount label -> value from the price-quote block.
            for r in rows:
                cells = ["" if c is None else str(c).strip() for c in r]
                for j, c in enumerate(cells):
                    if c and "Chiết khấu" in c:
                        discounts[c] = _f(r[3]) if len(r) > 3 else None
            # Extract anchor fields: unit code / type / area.
            ma = loai = dt = None
            for i, r in enumerate(rows):
                cells = ["" if c is None else str(c).strip() for c in r]
                if "Mã Căn" in cells:
                    for rr in rows[i + 1:i + 3]:
                        cc = ["" if c is None else str(c).strip() for c in rr]
                        if cc and len(cc) > 1 and cc[1] and "Mã" not in cc[1] and "BẢNG" not in cc[1]:
                            ma = cc[1]
                            break
                if "Loại Căn" in cells:
                    for rr in rows[i + 1:i + 3]:
                        cc = ["" if c is None else str(c).strip() for c in rr]
                        if cc and len(cc) > 3 and cc[3]:
                            loai = cc[3]
                            break
                if "Diện tích thông thủy" in cells:
                    for rr in rows[i + 1:i + 3]:
                        cc = ["" if c is None else str(c).strip() for c in rr]
                        if cc and len(cc) > 4 and cc[4]:
                            dt = cc[4]
                            break
            result[block][sn] = {"anchor_ma": ma, "anchor_loai": loai, "anchor_dt": dt, "discounts": discounts}
    return result


def main() -> None:
    products = extract_product_list()
    baskets = extract_priced_baskets()
    ptg = extract_ptg()

    (OUT / "product_list.json").write_text(
        json.dumps(products, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "priced_baskets.json").write_text(
        json.dumps(baskets, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "ptg.json").write_text(
        json.dumps(ptg, ensure_ascii=False, indent=1), encoding="utf-8")

    # Summary for the human reviewer.
    blocks = Counter(u["block"] for u in products)
    types = Counter(u["loai"] for u in products)
    print(f"product_list: {len(products)} units  blocks={dict(blocks)}")
    print(f"types: {dict(types)}")
    for name, lst in baskets.items():
        print(f"{name}: {len(lst)} priced units")
    for block, methods in ptg.items():
        print(f"PTG {block}: methods={list(methods)}")
    print("written ->", OUT)


if __name__ == "__main__":
    main()
