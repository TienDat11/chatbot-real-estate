"""Camellia document registry builder (Story 2.2).

Builds `ParsedDoc` objects for the `documents` registry from the confirmed
Camellia corpus under data/_processed, aligned with docs/data-contract.md.

Why not call load_document here: the loader replaces on re-ingest and would
DELETE the seeded facts behind docs like `price-camellia-2026q3`
(ingest/load.py, delete-before-insert). Story 2.2 only renders text, chunks
and validates required fields; the real registry write is Story 2.3
(ingest/run_camellia_ingest.py), which carries the seed facts through
load_document(..., preserve_seed_facts=True).
"""

from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field

from ingest.config import settings
from ingest.parser import ParsedDoc, ParsedSection

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DATA_DIR = _ROOT / "data" / "_processed"
_RAW_DIR = _DATA_DIR / "raw"

# Campaign + confirmation date (ground-truth confirmed 2026-08-13). Price/project
# docs open their interval that day — matches db/seed/camellia_rumor.sql so the
# `camellia-2026q3` campaign's source_doc_id FK keeps pointing at the same row.
CAMPAIGN = "camellia-2026q3"
PROJECT_KEY = "camellia"
CONFIRMED_DATE = _dt.date(2026, 8, 13)

# Price metadata shared by all price docs; trust='estimate' because all figures
# are du-doan ('Giá định hướng'), which caps answer confidence at MEDIUM.
_PRICE_META = {
    "project": PROJECT_KEY,
    "campaign": CAMPAIGN,
    "currency": "VND",
    "trust": "estimate",
}

_VISUAL_RE = re.compile(r"\[[^\]]*\]")  # [Logo], [GHI CHÚ OCR], [Stamp], [Watermark]

# Optical-noise line prefixes in the raw extract MDs that must not pollute the
# vector store; _clean_legal probes past a leading '- ' sidebar bullet.
_CLEAN_PREFIXES = (
    "Sidebar",
    "Logo",
    "Số lớn",
    "Visual:",
    "Tiêu đề:",
    "CƠ QUAN BAN HÀNH:",
    "SỐ HIỆU VĂN BẢN:",
    "Nội dung slide",
    "Nội dung văn bản",
    "[Các trang",
)
_BRAND_LINES = {
    "THE CAMELLIA",
    "SON TRA - DA NANG",
    "SƠN TRÀ - ĐÀ NẴNG",
    "PHÁT TRIỂN DỰ ÁN",
    "PHÁT TRIỂN KINH DOANH",
    "MBLAND",
    "WELAND REAL ESTATE",
    "WELAND",
}

# GT-B6 corpus corrections applied only inside the QnA text (feedback_data.txt Bảng 6).
_QNA_FIXES = (
    ("vay bù đắp khu mua", "vay bù đắp khi mua"),
    ("Hiện Đại (MBV)", "Hiện Đại (MBV - dự kiến)"),  # MBV join status = pending (B6 #4)
)
_QNA_DEPOSIT_NOTE = (
    "(Lưu ý: 50 ngày là thời hạn CHỦ ĐẦU TƯ gửi hồ sơ/giấy yêu cầu, KHÔNG phải thời hạn "
    "Nhà nước cấp sổ; việc cấp sổ thực tế có thể lâu hơn.)"
)


class RegistryBuildError(RuntimeError):
    """Registry could not be built — corpus file missing or structurally broken."""


@dataclass(frozen=True)
class DocumentDef:
    """One registry document: what to render + all required registry fields."""

    doc_id: str
    kind: str
    title: str
    source_path: pathlib.Path
    effective_from: _dt.date
    effective_to: _dt.date | None
    metadata: dict = field(default_factory=dict)
    sections: list[tuple[str | None, str]] = field(default_factory=list)


def _load_json(rel: str) -> dict:
    path = _DATA_DIR / rel
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryBuildError(f"Cannot read corpus JSON {path}: {exc}") from exc


def _file_hash(path: pathlib.Path) -> str:
    """SHA-256 of the source file bytes (AD-7 content addressing)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split_tail(text: str, cap: int) -> list[str]:
    """Hard-split an oversized section on newline boundaries near `cap`."""
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines():
        if buf and len(buf) + len(line) + 1 > cap:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def _sections(pairs: list[tuple[str | None, str]], cap: int) -> list[ParsedSection]:
    """Map (title, body) pairs to pre-chunked ParsedSections capped at `cap` chars."""
    out: list[ParsedSection] = []
    for title, body in pairs:
        text = body.strip()
        if not text:
            continue
        if len(text) <= cap:
            out.append(ParsedSection(text=text, section_title=title))
            continue
        out.extend(
            ParsedSection(text=piece.strip(), section_title=title)
            for piece in _split_tail(text, cap)
        )
    return out


def _render(defn: DocumentDef) -> ParsedDoc:
    """Turn a DocumentDef into a ParsedDoc with the required registry fields filled."""
    # Relative repo path so source_file stays stable across machines (data-contract §2).
    source_file = defn.source_path.relative_to(_ROOT).as_posix()
    return ParsedDoc(
        doc_id=defn.doc_id,
        title=defn.title,
        kind=defn.kind,
        source_file=source_file,
        sections=_sections(defn.sections, settings.chunk_cap),
        content_hash=_file_hash(defn.source_path),
        effective_from=defn.effective_from,
        effective_to=defn.effective_to,
        metadata=defn.metadata,
    )


# project docs — overview + QnA

def _project_overview() -> DocumentDef:
    data = _load_json("project_info.json")
    source = _DATA_DIR / "project_info.json"
    qm = data.get("quy_mo", {})
    co_cau = data.get("co_cau_can_ho", {})

    overview = [
        "- Dự án:", data.get("ten_phap_ly"),
        "- Tên thương mại:", data.get("ten_thuong_mai"),
        "- Vị trí:", data.get("vi_tri"),
        "- Chủ đầu tư:", data.get("chu_dau_tu"),
        "- Phát triển dự án:", data.get("phat_trien_du_an"),
        "- Phát triển kinh doanh:", data.get("phat_trien_kinh_doanh"),
        "- Quản lý vận hành:", data.get("quan_ly_van_hanh"),
        "- Thiết kế:", data.get("thiet_ke"),
        "- Thi công:", data.get("thi_cong"),
    ]
    quy_mo = [
        f"- Tổng diện tích khu đất: {qm.get('tong_dien_tich_dat_m2')} m2",
        f"- Diện tích xây dựng: {qm.get('dien_tich_xay_dung_m2')} m2 "
        f"(mật độ {qm.get('mat_do_xay_dung_pct')}%)",
        f"- Cây xanh: {qm.get('dien_tich_cay_xanh_m2')} m2; "
        f"- Sân đường nội bộ: {qm.get('dien_tich_san_duong_m2')} m2",
        f"- Số tầng: {qm.get('so_tang')}; chiều cao {qm.get('chieu_cao_m')} m",
        f"- Sản phẩm: {qm.get('so_can_ho')} căn hộ + {qm.get('so_can_tmdv')} căn TMDV "
        f"= {qm.get('tong_san_pham')} sản phẩm; {qm.get('can_ho_tren_tang')} căn/tầng",
        f"- Loại căn: {qm.get('so_loai_can')}; bán từ {qm.get('tang_ban')}",
    ]
    co_cau_lines = [f"- {k}: {v.get('so_can')} căn, diện tích {v.get('dien_tich_m2')} m2"
                    for k, v in co_cau.items()]
    co_cau_lines.append(f"- TMDV/shop-house: {data.get('tmdv_shophouse')}")

    tien_ich_area = ", ".join(
        f"{k} {v} m2" for k, v in data.get("dien_tich_tien_ich_m2", {}).items()
    )
    ha_tang = [f"- {k}: {v}" for k, v in data.get("ha_tang", {}).items()]
    ha_tang.extend(f"- {k}: {v}" for k, v in data.get("phi_dich_vu", {}).items())
    ha_tang.append(f"- Bàn giao: {data.get('ban_giao_noi_that')}")

    pl = data.get("phap_ly", {})
    phap_ly = [
        "- Sở hữu: " + str(pl.get("so_huu")),
        "- Ranh giới: " + str(pl.get("ranh_gioi")),
        "- Bán ngoài: " + str(pl.get("ban_ngoai")),
        "- Thanh toán pháp lý: " + str(pl.get("thanh_toan_phap_ly")),
        "- Sổ đỏ: " + str(pl.get("so_do")),
        "- Bảo hành: " + str(pl.get("bao_hanh")),
        "- Chuyển nhượng: " + str(pl.get("chuyen_nhuong")),
        "- Phí bảo trì: " + str(pl.get("kinh_phi_bao_tri")),
    ]
    sections = [
        ("Thông tin chung", "\n".join(o for o in overview if o)),
        ("Quy mô dự án", "\n".join(quy_mo)),
        ("Cơ cấu căn hộ", "\n".join(co_cau_lines)),
        ("Tiện ích", f"{data.get('tien_ich')}\n- Diện tích tiện ích: {tien_ich_area}"),
        ("Hạ tầng kỹ thuật và dịch vụ", "\n".join(ha_tang)),
        ("Pháp lý và sở hữu", "\n".join(phap_ly) + f"\n- Pháp lý: {pl.get('quyet_dinh_254')}"),
        ("Liên hệ", "\n".join(f"- {k}: {v}" for k, v in data.get("lien_he", {}).items())),
    ]
    metadata = {
        "project_name": data.get("ten_thuong_mai"),
        "location": data.get("vi_tri"),
        "developer": data.get("chu_dau_tu"),
        "total_units": qm.get("so_can_ho"),
        "handover_date": data.get("thoi_gian", {}).get("ban_giao"),
        "amenities": [
            "sảnh chuẩn khách sạn", "nhà trẻ", "thư viện", "Business Lounge",
            "gym", "yoga", "bể bơi trong nhà", "Event Ballroom", "Kid Club",
            "Sky Park 6 vườn chủ đề", "shophouse",
        ],
        "legal_status": "đang triển khai",
        "trust": "rumor",
    }
    return DocumentDef(
        doc_id="project-camellia-2026q3",
        kind="project",
        title="Hồ sơ tổng quan dự án The Camellia Sơn Trà - Đà Nẵng",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


_QNA_HEADING_RE = re.compile(r"^##\s+Trang\s+(\d+)", re.MULTILINE)
# Matches top-level (Q10) and sub-numbered (Q13.2) questions. The page keep-filter
# and the section split must both accept sub-numbering or whole pages drop.
_QNA_QUESTION_RE = re.compile(r"^Q(\d+(?:\.\d+)*)\s*[:.]\s*(.*)$", re.MULTILINE)


@functools.lru_cache(maxsize=1)
def _qna_blocks() -> list[tuple[int, str]]:
    """Split qna_extract.md into (page_number, page_text) blocks."""
    path = _RAW_DIR / "qna_extract.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryBuildError(f"Cannot read {path}: {exc}") from exc
    pages = _QNA_HEADING_RE.split(raw)[1:]  # [num, body, num, body, ...]
    blocks: list[tuple[int, str]] = []
    for i in range(0, len(pages) - 1, 2):
        page_no = int(pages[i])
        body = pages[i + 1]
        # Page numbering resets per section (many Q1), so keep any page with a
        # Q/A pair — sub-numbered pages included; cover pages carry none.
        if _QNA_QUESTION_RE.search(body):
            blocks.append((page_no, body))
    return blocks


def _qna_doc() -> DocumentDef:
    """Render each Q/A pair as its own section, applying the GT-B6 corpus fixes."""
    sections: list[tuple[str | None, str]] = []
    for page_no, body in _qna_blocks():
        question_match = list(_QNA_QUESTION_RE.finditer(body))
        for idx, m in enumerate(question_match):
            q_text = m.group(2).strip()
            end = question_match[idx + 1].start() if idx + 1 < len(question_match) else len(body)
            answer = body[m.end():end].strip()
            answer = _VISUAL_RE.sub("", answer).strip()
            for old, new in _QNA_FIXES:
                answer = answer.replace(old, new)
            if "nộp hồ sơ đề nghị cơ quan nhà nước" in answer:
                answer = f"{answer}\n{_QNA_DEPOSIT_NOTE}"
            title = f"Trang {page_no} — Q{m.group(1)}"
            sections.append((title, f"{q_text}\n{answer}"))
    return DocumentDef(
        doc_id="project-camellia-qna",
        kind="project",
        title="Bộ câu hỏi giải đáp thông tin dự án The Camellia (QnA)",
        source_path=_RAW_DIR / "qna_extract.md",
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=_project_meta()
        | {"origin": "qna_extract", "source": "data/_processed/raw/qna_extract.md"},
        sections=sections,
    )


def _project_meta() -> dict:
    data = _load_json("project_info.json")
    return {
        "project_name": data.get("ten_thuong_mai"),
        "location": data.get("vi_tri"),
        "developer": data.get("chu_dau_tu"),
        "total_units": data.get("quy_mo", {}).get("so_can_ho"),
        "handover_date": data.get("thoi_gian", {}).get("ban_giao"),
        "legal_status": "đang triển khai",
        "trust": "rumor",
    }


# price docs — matrix + payment schedule + discount policy

# Method key mismatch between sources: price_matrix.json uses 'thanh_thoi',
# business_rules.json uses 'thanhthoi'. Resolve with a normalizing fallback.
_METHOD_LABEL = {
    "htls": "HTLS",
    "thanh_thoi": "Thảnh thơi",
    "chuan": "Thanh toán chuẩn",
    "som95": "Sớm 95%",
}
_METHOD_NORMALIZED = {k: k.replace("_", "") for k in _METHOD_LABEL}


def _price_matrix_doc() -> DocumentDef:
    """Directional price sheet: per unit type x 4 payment methods (min-max tỷ đồng)."""
    data = _load_json("price_matrix.json")
    source = _DATA_DIR / "price_matrix.json"
    note = data.get("note", "")
    floor_rule = data.get("floor_rule", "")
    warnings = data.get("interpolation_warning", "")
    findings = "\n".join(f"- {f}" for f in data.get("findings_flag", []))

    sections: list[tuple[str | None, str]] = [
        (
            "Ghi chú chung",
            f"- Đơn vị: {data.get('unit')}\n- {note}\n- Quy tắc tầng: {floor_rule}\n- {warnings}"
            + (f"\n{findings}" if findings else ""),
        )
    ]
    for t in data.get("types", []):
        gia_lines = []
        for key, label in _METHOD_LABEL.items():
            lo, hi = t.get("gia", {}).get(key, (None, None))
            if lo is not None and hi is not None:
                gia_lines.append(f"- {label}: {lo} - {hi} tỷ đồng")
        body = [
            f"- Loại căn: {t['loai']}",
            f"- Mã căn (nhãn): {', '.join(t.get('ma_can', []))}",
            f"- Diện tích thông thủy: {t.get('dien_tich_thong_thuy_m2')} m2",
        ]
        for code, info in t.get("vi_du_can", {}).items():
            body.append(
                f"  * {code}: {info.get('thong_thuy')} m2 thông thủy / "
                f"{info.get('tim_tuong')} m2 tim tường"
            )
        body.extend(gia_lines)
        if t.get("note"):
            body.append(f"- Ghi chú: {t['note']}")
        sections.append((f"Giá loại căn {t['loai']}", "\n".join(body)))

    metadata = dict(_PRICE_META)
    metadata.update({
        "price_structure": "directional-price",
        "source": source.relative_to(_ROOT).as_posix(),
    })
    return DocumentDef(
        doc_id="price-camellia-2026q3",
        kind="price",
        title="Bảng giá định hướng The Camellia Q3/2026 theo phương thức thanh toán",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


def _payment_doc() -> DocumentDef:
    """4 payment schedules + booking/deposit, rendered from payment_methods.json."""
    data = _load_json("payment_methods.json")
    source = _DATA_DIR / "payment_methods.json"
    sections: list[tuple[str | None, str]] = []

    bd = data.get("booking_and_deposit", {})
    sections.append(
        (
            "Booking và tiền cọc",
            "\n".join(
                f"- {k}: {v}"
                for k, v in {
                    "Phí booking": bd.get("booking_fee_vnd"),
                    "Ghi chú booking": bd.get("booking_note"),
                    "Tiền cọc (TTĐC)": bd.get("deposit_total_vnd"),
                    "Ghi chú cọc": bd.get("deposit_note"),
                    "Cọc trong vốn tự có thảnh thơi": bd.get("deposit_in_equity_thangthoi"),
                    "Công thức": bd.get("base_formula"),
                }.items()
                if v is not None
            ),
        )
    )

    for m in data.get("methods", []):
        body = [
            f"- Nguồn vốn: {m.get('nguon_von')}",
        ]
        if m.get("condition"):
            body.append(f"- Điều kiện: {m['condition']}")
        if m.get("note_ck"):
            body.append(f"- Chiết khấu: {m['note_ck']}")
        if m.get("interest_note"):
            body.append(f"- Lãi suất: {m['interest_note']}")
        if m.get("note_eb"):
            body.append(f"- Early booking: {m['note_eb']}")
        for ms in m.get("milestones", []):
            pct = f"{ms['pct']}%" if ms.get("pct") is not None else ms.get("amount", "-")
            extra = f" ({ms['extra']})" if ms.get("extra") else ""
            body.append(f"  {ms.get('order', '?')}. {ms.get('milestone')} — {pct}{extra}")
        sections.append((f"Phương thức: {m.get('name')}", "\n".join(body)))

    xlsx = data.get("xlsx_demo", {})
    if xlsx:
        lines = [
            f"- File: {xlsx.get('file')}",
            f"- Căn mẫu: {xlsx.get('unit')}",
            f"- Giá bán (min, VAT+KPBT): {xlsx.get('gia_ban_min_vat_kpbt')} đ",
            f"- Ghi chú: {xlsx.get('note_base')}",
        ]
        sections.append(("Bài toán đầu tư mẫu (XLSX)", "\n".join(lines)))
    if data.get("open_questions"):
        sections.append(
            ("Câu hỏi còn mở của data owner",
             "\n".join(f"- {q}" for q in data["open_questions"]))
        )

    metadata = dict(_PRICE_META)
    metadata.update({
        "price_structure": "payment-schedule",
        "source": source.relative_to(_ROOT).as_posix(),
    })
    return DocumentDef(
        doc_id="price-camellia-2026q3-payment",
        kind="price",
        title="Lịch thanh toán 4 phương thức — The Camellia Q3/2026",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


def _policy_doc() -> DocumentDef:
    """Chính sách bán hàng: ma trận chiết khấu + early booking + cọc + HTLS
    (business_rules.json)."""
    data = _load_json("business_rules.json")
    source = _DATA_DIR / "business_rules.json"
    rules = data.get("rules", {})
    sections: list[tuple[str | None, str]] = []

    dm = rules.get("discount_matrix", {})
    entries = []
    for key, label in _METHOD_LABEL.items():
        # business_rules.json keys 'thanhthoi' (no underscore) — normalize.
        item = dm.get(key) or dm.get(_METHOD_NORMALIZED[key])
        if not item:
            raise RegistryBuildError(f"discount_matrix missing method {key!r}")
        entries.append(
            f"- {label}: CK phương thức {item.get('ck_phuong_thuc')}%, EB {item.get('eb', '—')}%, "
            f"tổng khi EB {item.get('total_when_eb', item.get('total', '—'))}% — "
            f"{item.get('note', '')}"
        )
    sections.append(("Ma trận chiết khấu (cộng dồn với early booking)", "\n".join(entries)))

    eb = rules.get("early_booking", {})
    dp = rules.get("deposit", {})
    htls = rules.get("htls", {})
    sections.append((
        "Early booking",
        f"- {eb.get('value')}\n- Phí đặt booking: {eb.get('fee_vnd')} đ ({eb.get('fee_note')})",
    ))
    sections.append((
        "Tiền cọc (TTĐC)",
        f"- {dp.get('total_vnd')} đ — {dp.get('note')}\n"
        f"- Ghi chú: {dp.get('in_equity_note')}\n"
        f"- Quy tắc cơ sở: {dp.get('base_rule')}",
    ))
    sections.append((
        "Chính sách HTLS (hỗ trợ lãi suất)",
        f"- Ngân hàng: {', '.join(htls.get('banks', []))}\n"
        f"- {htls.get('interest_note')}\n"
        f"- Vay tối đa {htls.get('vay_max_pct')}% trong {htls.get('vay_term_months')} tháng\n"
        f"- Ân hạn nợ gốc: {htls.get('grace_note')}",
    ))
    for key in ("sale_floor", "floor_increment", "price_quality", "price_float"):
        item = rules.get(key, {})
        if item:
            sections.append((
                item.get("label", key),
                f"- {item.get('value')}\n- {item.get('note')}",
            ))
    contact_item = rules.get("contacts_update")
    if contact_item:
        sections.append((
            contact_item.get("label", "Môi giới"),
            f"- {contact_item.get('value')}\n- {contact_item.get('note')}",
        ))

    metadata = dict(_PRICE_META)
    metadata.update({
        "price_structure": "discount-policy",
        "source": source.relative_to(_ROOT).as_posix(),
    })
    return DocumentDef(
        doc_id="price-camellia-2026q3-policy",
        kind="price",
        title="Chính sách chiết khấu và hỗ trợ tài chính — The Camellia Q3/2026",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


# legal docs — GCN ground + QĐ 254/191 + CV 12779/SXD-QLN

@functools.lru_cache(maxsize=1)
def _legal_pages() -> dict[int, str]:
    """Split legal_extract.md into page blocks and strip visual/OCR noise."""
    path = _RAW_DIR / "legal_extract.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryBuildError(f"Cannot read {path}: {exc}") from exc

    pages = _QNA_HEADING_RE.split(raw)[1:]
    result: dict[int, str] = {}
    for i in range(0, len(pages) - 1, 2):
        result[int(pages[i])] = pages[i + 1]
    return result


def _clean_legal(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Sidebar bullets and brand/watermark noise start with '- '; probe past it.
        probe = line[2:].strip() if line.startswith("- ") else line
        if probe.startswith(_CLEAN_PREFIXES):
            continue
        if probe in _BRAND_LINES:
            continue
        line = _VISUAL_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _legal_doc(
    doc_id: str,
    title: str,
    pages: list[int],
    metadata: dict,
    effective_from: _dt.date,
) -> DocumentDef:
    """One section per source page so multi-parcel docs (GCN thửa 1+2) keep full content."""
    pages_map = _legal_pages()
    sections = [
        (f"Trang {page_no}", _clean_legal(pages_map.get(page_no, "")))
        for page_no in pages
        if _clean_legal(pages_map.get(page_no, ""))
    ]
    return DocumentDef(
        doc_id=doc_id,
        kind="legal",
        title=title,
        source_path=_RAW_DIR / "legal_extract.md",
        effective_from=effective_from,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


def _legal_metadata(
    number: str,
    doc_type: str,
    issuer: str,
    issue_date: str,
    keywords: list[str],
    related: list[str] | None = None,
) -> dict:
    meta = {
        "document_number": number,
        "document_type": doc_type,
        "issuer": issuer,
        "issue_date": issue_date,
        "keywords": keywords,
    }
    if related:
        meta["related_docs"] = related
    return meta


def _legal_docs() -> list[DocumentDef]:
    return [
        _legal_doc(
            "legal-gcnqsd-2011",
            "Giấy chứng nhận quyền sử dụng đất thửa 1+2 (CT09441 / CT09442)",
            [3, 4],
            _legal_metadata(
                "CT09441 / CT09442", "GCNQSDD", "UBND TP Đà Nẵng", "2011-10-04",
                ["GCNQSD đất", "thửa đất 1+2", "lô A2-1", "diện tích đất 4299.9 m2"],
                ["legal-qd254-2024", "legal-qd191-2025"],
            ),
            _dt.date(2011, 10, 4),
        ),
        _legal_doc(
            "legal-qd254-2024",
            "Quyết định 254/QĐ-UBND chấp thuận chủ trương đầu tư (31/01/2024)",
            [5],
            _legal_metadata(
                "254/QĐ-UBND", "quyet-dinh", "UBND TP Đà Nẵng", "2024-01-31",
                ["chấp thuận chủ trương", "chấp thuận nhà đầu tư", "chủ trương đầu tư"],
                ["legal-gcnqsd-2011", "legal-qd191-2025"],
            ),
            _dt.date(2024, 1, 31),
        ),
        _legal_doc(
            "legal-qd191-2025",
            "Quyết định 191/QĐ-UBND phê duyệt quy hoạch tổng mặt bằng 1/500 (22/01/2025)",
            [6],
            _legal_metadata(
                "191/QĐ-UBND", "quyet-dinh", "UBND quận Sơn Trà", "2025-01-22",
                ["quy hoạch tổng mặt bằng", "1/500", "hệ số SDĐ 11.09"],
                ["legal-qd254-2024"],
            ),
            _dt.date(2025, 1, 22),
        ),
        _legal_doc(
            "legal-cv12779-2026",
            "Công văn 12779/SXD-QLN đủ điều kiện bán nhà ở hình thành trong tương lai (13/07/2026)",
            [7],
            _legal_metadata(
                "12779/SXD-QLN", "cong-van", "Sở Xây dựng Đà Nẵng", "2026-07-13",
                ["nhà ở hình thành trong tương lai", "đủ điều kiện bán", "Điều 24"],
                ["legal-qd254-2024", "legal-qd191-2025"],
            ),
            _dt.date(2026, 7, 13),
        ),
    ]


# public API (builder entry points)

def build_documents() -> list[ParsedDoc]:
    """Build every Camellia registry document from the confirmed processed corpus."""
    defs = (
        [_project_overview(), _qna_doc()]
        + [_price_matrix_doc(), _payment_doc(), _policy_doc()]
        + _legal_docs()
    )
    return [_render(d) for d in defs]


REQUIRED_FIELDS = ("doc_id", "kind", "title", "source_file", "effective_from", "content_hash")


def validate_document(doc: ParsedDoc) -> list[str]:
    """Return missing required fields for this document (empty list = valid)."""
    missing = [f for f in REQUIRED_FIELDS if not getattr(doc, f)]
    if doc.kind not in ("legal", "price", "project"):
        missing.append(f"kind invalid: {doc.kind}")
    if not (doc.sections and all(s.text.strip() for s in doc.sections)):
        missing.append("content: at least one non-empty section")
    if isinstance(doc.effective_from, _dt.datetime):
        missing.append("effective_from must be a date, got datetime")
    return missing


# CLI

def _dry_run_report(docs: list[ParsedDoc]) -> str:
    columns = ("doc_id", "kind", "title", "eff_from", "eff_to", "sections", "chars")
    rows: list[list[str]] = []
    for d in sorted(docs, key=lambda x: (x.kind, x.doc_id)):
        rows.append([
            d.doc_id,
            d.kind,
            d.title[:34],
            str(d.effective_from),
            str(d.effective_to or "open"),
            str(len(d.sections)),
            str(sum(len(s.text) for s in d.sections)),
        ])
    widths = [max(len(r[i]) for r in [columns, *rows]) for i in range(len(columns))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*columns)]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(fmt.format(*r) for r in rows)
    return "\n".join(lines)


async def _check_db_insert(docs: list[ParsedDoc], settings_) -> None:
    """Verify every registry row is insertable (documents only) in a ROLLED-BACK transaction.

    asyncpg commits on clean transaction exit — rollback explicitly or the check
    pollutes the live registry with orphaned version-1 rows.
    """
    import asyncpg

    conn = await asyncpg.connect(settings_.pg_dsn)
    tx = conn.transaction()
    await tx.start()
    try:
        for d in docs:
            await conn.execute(
                """
                INSERT INTO documents (
                  doc_id, kind, title, source_file, effective_from, effective_to,
                  status, content_hash, version, metadata
                ) VALUES ($1,$2,$3,$4,$5,$6,'published',$7,1,$8::jsonb)
                ON CONFLICT (doc_id) DO NOTHING
                """,
                d.doc_id, d.kind, d.title, d.source_file, d.effective_from,
                d.effective_to, d.content_hash, json.dumps(d.metadata, ensure_ascii=False),
            )
        print("DB check: all registry rows insert cleanly (rolled back, no data written)")
        await tx.rollback()
    except Exception as exc:  # noqa: BLE001
        await tx.rollback()
        print(f"DB check FAILED: {exc}")
        raise
    finally:
        await conn.close()


def main() -> int:
    """CLI: build + validate the registry. `--check-db` also verifies inserts (needs PG up)."""
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(
        description="Build + validate Camellia document registry (Story 2.2)."
    )
    ap.add_argument(
        "--check-db",
        action="store_true",
        help="Verify inserts against the live registry DB (documents only, rolled back).",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit the validated registry as compact JSON lines.",
    )
    args = ap.parse_args()

    docs = build_documents()
    problems: list[str] = []
    for d in docs:
        missing = validate_document(d)
        if missing:
            problems.append(f"{d.doc_id}: {', '.join(missing)}")

    if args.json:
        import json as _json

        for d in docs:
            print(_json.dumps({
                "doc_id": d.doc_id, "kind": d.kind, "title": d.title,
                "source_file": d.source_file, "effective_from": str(d.effective_from),
                "effective_to": str(d.effective_to), "status": "published",
                "content_hash": d.content_hash,
                "version": 1, "metadata": d.metadata,
                "section_count": len(d.sections),
            }, ensure_ascii=False))
        return 1 if problems else 0

    print(_dry_run_report(docs))
    print(f"\nTổng docs: {len(docs)}")
    if problems:
        print("\nVALIDATION FAILED:")
        for p in problems:
            print(f"  ❌ {p}")
        return 1
    print("VALIDATION: PASS — mọi document đủ field bắt buộc theo data-contract.")
    if args.check_db:
        asyncio.run(_check_db_insert(docs, settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
