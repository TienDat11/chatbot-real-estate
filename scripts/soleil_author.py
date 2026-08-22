"""Author the deterministic Soleil canonical JSONs from the XLSX extracts.

Writes data/_processed/soleil/{unit_catalog,price_matrix,payment_methods,
sales_contacts}.json in the Camellia schema vocabulary. These four are fully
deterministic (spreadsheet cells); project_info and business_rules are curated
separately because they draw on OCR'd legal/handover PDFs.
"""

from __future__ import annotations

import json
import pathlib
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXT = ROOT / "data" / "_processed" / "soleil" / "_extract"
OUT = ROOT / "data" / "_processed" / "soleil"
OUT.mkdir(parents=True, exist_ok=True)

PROJECT = "The Soleil Đà Nẵng"


def _num(x):
    return x if isinstance(x, (int, float)) else None


def _load(name):
    with (EXT / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def _r(x, nd=2):
    return round(x, nd) if x is not None else None


# ---------------------------------------------------------------- unit catalog

LOAI_KY_HIEU = {
    "Stu": "Căn hộ Studio",
    "Pan": "Căn hộ Panorama",
    "Hpan": "Căn hộ Hyper Panorama",
    "HPan2": "Căn hộ Hyper Panorama 2 phòng ngủ",
    "S-Hpan": "Căn hộ Super Hyper Panorama",
    "1BR": "Căn hộ 1 phòng ngủ",
    "2BR": "Căn hộ 2 phòng ngủ",
    "Pent": "Penthouse",
}


def build_unit_catalog():
    products = _load("product_list.json")
    blocks = defaultdict(list)
    for u in products:
        blocks[u["block"]].append(u)

    out_units = []
    for u in products:
        out_units.append({
            "ma_sp": u["ma_sp"],
            "block": u["block"],
            "tang": u["tang"],
            "so_can": u["so_can"],
            "loai": u["loai"],
            "dien_tich_m2": _r(u["dien_tich_m2"]),
            "vi_tri": u["vi_tri"],
            "huong": u["huong"],
        })

    summary = {}
    for block, us in sorted(blocks.items()):
        by_type = defaultdict(list)
        for u in us:
            by_type[u["loai"]].append(u)
        summary[block] = {
            "tong_can": len(us),
            "co_cau": {
                t: {
                    "so_can": len(v),
                    "dien_tich_m2": [min(_num(x["dien_tich_m2"]) for x in v),
                                     max(_num(x["dien_tich_m2"]) for x in v)],
                }
                for t, v in sorted(by_type.items())
            },
        }

    return {
        "project": PROJECT,
        "source": "2025.10.02 Bảng tính dòng tiền (PA tự vận hành, tòa A1) + "
                  "2025.11.14 Bảng tính dòng tiền (PA ủy thác, tòa D) — sheet 'Danh sách sản phẩm'",
        "note": "Catalog đầy đủ 1087 căn (tòa A1 779 + tòa D 308). Diện tích là diện tích "
                "thông thủy. Tầng A1 từ 4 đến 52 (+ penthouse 54), tòa D từ 10 đến 45.",
        "loai_ky_hieu": LOAI_KY_HIEU,
        "blocks": summary,
        "units": out_units,
    }


# ------------------------------------------------------------------ price matrix

def build_price_matrix():
    baskets = _load("priced_baskets.json")

    def agg(tower_key):
        agg_map = defaultdict(lambda: {"prices": [], "areas": [], "codes": []})
        for name, lst in baskets.items():
            for u in lst:
                if name == "dxdh":
                    tower = "SLD" if u.get("toa") == "SLD" else "SLA1"
                else:
                    tower = u.get("toa")
                if tower != tower_key:
                    continue
                p = _num(u["gia_gom_vat_chua_kpbt"])
                a = _num(u["dien_tich_thong_thuy"])
                agg_map[u["loai"]]["codes"].append(u["ma_can"])
                if p is not None:
                    agg_map[u["loai"]]["prices"].append(p)
                if a is not None:
                    agg_map[u["loai"]]["areas"].append(a)
        return agg_map

    types = []
    for tower_key, tower_name in [("SLA1", "Tòa A1"), ("SLD", "Tòa D")]:
        agg_map = agg(tower_key)
        for loai, d in sorted(agg_map.items()):
            prices = d["prices"]
            areas = d["areas"]
            if not prices:
                continue
            ppm = [p / a / 1e9 for p, a in zip(prices, areas)] if len(prices) == len(areas) else []
            types.append({
                "block": tower_key,
                "ten_toa": tower_name,
                "loai": loai,
                "ma_can_mau": d["codes"],
                "dien_tich_thong_thuy_m2": [round(min(areas), 1), round(max(areas), 1)] if areas else None,
                "gia": {
                    "tong_vnd": [round(min(prices)), round(max(prices))],
                    "ty": [round(min(prices) / 1e9, 2), round(max(prices) / 1e9, 2)],
                },
                "gia_m2_ty": [round(min(ppm), 3), round(max(ppm), 3)] if ppm else None,
            })

    return {
        "project": PROJECT,
        "source": "Giỏ Hàng Chung 2025.12.28 (A1+D) + Giỏ Hàng ĐXDH 2025.12.21 — giá sự kiện "
                  "từng căn (38 căn có giá)",
        "unit": "tỷ đồng",
        "note": "Giá là GIÁ SỰ KIỆN (event basket) cho 38 căn mẫu có giá, KHÔNG phải bảng giá "
                "đầy đủ 1087 căn. Giá gồm VAT, chưa gồm KPBT (2%). Giá sự kiện = giá sau chiết "
                "khấu đã áp dụng. Tòa D đắt hơn tòa A1 theo đơn giá m².",
        "types": types,
        "price_per_m2_warning": "Đơn giá m² là khoảng suy từ 38 căn mẫu có giá; không nội suy "
                                "tuyến tính cho mọi căn/tầng.",
    }


# -------------------------------------------------------------- payment methods

def build_payment_methods():
    # Discounts (fractions) per block x method, from PTG.
    def dsc(block):
        ptg = _load("ptg.json")[block]
        out = {}
        for m, info in ptg.items():
            base = 0.0
            for k, v in info["discounts"].items():
                if isinstance(v, (int, float)):
                    base += v
            out[m] = round(base, 3)
        return out

    a1 = dsc("A1")
    d = dsc("D")

    methods = [
        {
            "key": "tttd",
            "name": "Thanh toán tiến độ thông thường (TTTĐ)",
            "ck_phuong_thuc_pct": {"A1": 3.0, "D": 1.0},
            "ck_khuyen_mai_pct": {"A1": 2.0, "D": 2.0, "note": "A1: Early Bird; D: Năm Du Lịch 2026"},
            "tong_ck_pct": {"A1": 5.0, "D": 3.0},
            "milestones": [
                {"order": 1, "milestone": "Đặt cọc / Ký TTĐC", "amount": "100 triệu đồng", "pct": None,
                 "note": "Trong vòng 24h kể từ khi khóa căn hộ"},
                {"order": 2, "milestone": "Đợt 1 - Ký HĐMB", "pct": 15.0, "note": "Trong 10 ngày từ đặt cọc"},
                {"order": 3, "milestone": "Đợt 2", "pct": 15.0, "note": "Trong 30 ngày từ Đợt 1 (gồm tiền đặt cọc)"},
                {"order": 4, "milestone": "Đợt 3-8 (6 đợt)", "pct": 2.0, "note": "2%/đợt, mỗi 30 ngày"},
                {"order": 5, "milestone": "Đợt 9-14 (6 đợt)", "pct": 3.0, "note": "3%/đợt, mỗi 45 ngày"},
                {"order": 6, "milestone": "Đợt 15", "pct": 10.0, "note": "Trong 30 ngày từ Đợt 14"},
                {"order": 7, "milestone": "Đợt 16 - Nhận bàn giao (dự kiến Q4/2027)", "pct": 25.0,
                 "note": "25% GTCH (gồm VAT) + 100% KPBT + VAT của 5% GTCH"},
                {"order": 8, "milestone": "Đợt 17 - Cấp GCN quyền sở hữu", "pct": 5.0,
                 "note": "5% GTCH (không gồm VAT)"},
            ],
        },
        {
            "key": "ttdb",
            "name": "Thanh toán đặc biệt (TTĐB) — chỉ tòa A1",
            "only_block": "A1",
            "ck_phuong_thuc_pct": {"A1": 1.0},
            "ck_khuyen_mai_pct": {"A1": 2.0, "note": "Early Bird"},
            "tong_ck_pct": {"A1": 3.0},
            "milestones": [],
        },
        {
            "key": "tts95",
            "name": "Thanh toán sớm 95% (TTS 95%)",
            "ck_phuong_thuc_pct": {"A1": 11.5, "D": 5.0},
            "ck_khuyen_mai_pct": {"A1": 2.0, "D": 2.0, "note": "A1: Early Bird; D: Năm Du Lịch 2026"},
            "tong_ck_pct": {"A1": 13.5, "D": 7.0},
            "milestones": [],
        },
        {
            "key": "tts70",
            "name": "Thanh toán sớm 70% (TTS 70%)",
            "ck_phuong_thuc_pct": {"A1": 7.0, "D": 3.0},
            "ck_khuyen_mai_pct": {"A1": 2.0, "D": 2.0, "note": "A1: Early Bird; D: Năm Du Lịch 2026"},
            "tong_ck_pct": {"A1": 9.0, "D": 5.0},
            "milestones": [],
        },
        {
            "key": "tts50",
            "name": "Thanh toán sớm 50% (TTS 50%) — chỉ tòa A1",
            "only_block": "A1",
            "ck_phuong_thuc_pct": {"A1": 5.0},
            "ck_khuyen_mai_pct": {"A1": 2.0, "note": "Early Bird"},
            "tong_ck_pct": {"A1": 7.0},
            "milestones": [],
        },
        {
            "key": "htls",
            "name": "Hỗ trợ lãi suất (HTLS)",
            "ck_phuong_thuc_pct": {"A1": 0.0, "D": 0.0},
            "ck_khuyen_mai_pct": {"A1": 2.0, "D": 2.0, "note": "A1: Early Bird; D: Năm Du Lịch 2026"},
            "tong_ck_pct": {"A1": 2.0, "D": 2.0},
            "milestones": [],
        },
    ]

    return {
        "project": PROJECT,
        "source_dir": "data/soleil",
        "extract_method": "openpyxl (XLSX) — PTG A1 2026.03.03 + PTG D 2026.05.08",
        "note": "Chiết khấu gồm 2 phần: (1) khuyến mại chung (A1 'Early Bird' 2% / D 'Năm Du Lịch 2026' 2%), "
                "(2) chiết khấu theo phương thức thanh toán. Cộng dồn. Lịch thanh toán 17 đợt trích từ "
                "PTG A1 TTTĐ (căn mẫu SLA1). Mốc bàn giao dự kiến Q4/2027.",
        "booking_and_deposit": {
            "deposit_vnd": 100000000,
            "deposit_note": "Đặt cọc 100 triệu đồng, trong vòng 24h kể từ khi khóa căn hộ; khoản cọc được "
                            "khấu trừ vào Đợt 2.",
        },
        "chuyen_khoan": {
            "chu_tai_khoan": "Công ty cổ phần PPC An Thịnh Đà Nẵng",
            "so_tai_khoan": "112646755555",
            "ngan_hang": "Vietinbank - CN Đà Nẵng",
            "cu_phap": "KH [Tên đầy đủ] - thanh toan [Đợt thanh toán] - can [mã sản phẩm] - DA THE SOLEIL DN",
        },
        "methods": methods,
    }


# -------------------------------------------------------------- sales contacts

def build_sales_contacts():
    return {
        "project": PROJECT,
        "note": "Danh sách liên hệ từ hồ sơ pháp lý + bảng giá. Chưa có danh bạ môi giới riêng "
                "(khác Camellia) — cập nhật khi có dữ liệu.",
        "chu_dau_tu": {
            "ten": "Công ty Cổ phần PPC An Thịnh Đà Nẵng",
            "gcn_dkkd": "0401622745",
            "tru_so": "02 Phạm Văn Đồng, phường Phước Mỹ, quận Sơn Trà, Đà Nẵng",
            "nguoi_dai_dien_phap_luat": "Nguyễn Kháng Chiến (Chủ tịch HĐQT)",
            "ngan_hang_giao_dich": "Vietinbank - CN Đà Nẵng, STK 112646755555",
        },
        "contacts": [],
    }


def main():
    (OUT / "unit_catalog.json").write_text(
        json.dumps(build_unit_catalog(), ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "price_matrix.json").write_text(
        json.dumps(build_price_matrix(), ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "payment_methods.json").write_text(
        json.dumps(build_payment_methods(), ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "sales_contacts.json").write_text(
        json.dumps(build_sales_contacts(), ensure_ascii=False, indent=1), encoding="utf-8")

    uc = build_unit_catalog()
    pm = build_price_matrix()
    print("unit_catalog.json:", len(uc["units"]), "units | blocks:", {k: v["tong_can"] for k, v in uc["blocks"].items()})
    print("price_matrix.json:", len(pm["types"]), "type rows")
    print("payment_methods.json:", len(build_payment_methods()["methods"]), "methods")
    print("sales_contacts.json written")
    print("->", OUT)


if __name__ == "__main__":
    main()
