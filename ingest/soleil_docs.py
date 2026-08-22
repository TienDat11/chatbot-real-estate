"""Soleil document registry builder (mirror of ingest.camellia_docs.py).

Builds `ParsedDoc` objects for the `documents` registry from the confirmed Soleil
corpus under data/_processed/soleil. The seed-carrier doc `price-soleil-2026q3`
is rendered here too; its facts are owned by db/seed/soleil_campaign.sql and the
loader preserves them (ingest/run_soleil_ingest.py, preserve_seed_facts=True).
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
_DATA_DIR = _ROOT / "data" / "_processed" / "soleil"
_OCR_DIR = _DATA_DIR / "_extract" / "ocr"

# Campaign + confirmation date (Soleil OCR ingested 2026-08-21). Price/project
# docs open their interval that day - matches db/seed/soleil_campaign.sql so the
# `soleil-2026q3` campaign's source_doc_id FK keeps pointing at the same row.
CAMPAIGN = "soleil-2026q3"
PROJECT_KEY = "soleil"
CONFIRMED_DATE = _dt.date(2026, 8, 21)

# Price metadata shared by all price docs; trust='estimate' because the event
# basket carries a partial price grid (38 sample units, not the full 1087).
_PRICE_META = {
    "project": PROJECT_KEY,
    "campaign": CAMPAIGN,
    "currency": "VND",
    "trust": "estimate",
}


class RegistryBuildError(RuntimeError):
    """Registry could not be built - corpus file missing or structurally broken."""


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


def _load_json(name: str) -> dict:
    path = _DATA_DIR / name
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


# OCR text cleaning (Soleil OCR is noisy: image tokens, page numbers, separators).

_IMAGE_RE = re.compile(r"<!--\s*image\s*-->")
_PAGE_NUM_RE = re.compile(r"^\d{1,3}$")
_SEPARATOR_RE = re.compile(r"^\s*\*\s*\*\s*\*\s*$")


def _clean_ocr(text: str) -> str:
    """Strip image tokens, standalone page numbers and separator lines from OCR text."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _IMAGE_RE.search(line) or _PAGE_NUM_RE.match(line) or _SEPARATOR_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def _read_ocr(filename: str) -> str:
    path = _OCR_DIR / filename
    try:
        return _clean_ocr(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryBuildError(f"Cannot read OCR file {path}: {exc}") from exc


# project docs - overview + QnA

def _project_overview() -> DocumentDef:
    data = _load_json("project_info.json")
    source = _DATA_DIR / "project_info.json"
    qm = data.get("quy_mo", {})
    toa_d = qm.get("toa_D", {})
    toa_a1 = qm.get("toa_A1", {})

    overview = [
        "- Tên pháp lý:", data.get("ten_phap_ly"),
        "- Tên thương mại:", data.get("ten_thuong_mai"),
        "- Vị trí:", data.get("vi_tri"),
        "- Chủ đầu tư:", data.get("chu_dau_tu"),
        "- Quản lý vận hành:", data.get("quan_ly_van_hanh"),
        "- Thiết kế:", data.get("thiet_ke"),
        "- Thi công:", data.get("thi_cong"),
    ]
    quy_mo = [
        f"- Tổng diện tích khu đất: {qm.get('tong_dien_tich_dat_m2')} m2",
        f"- Hệ số SDĐ: {qm.get('he_so_sdd')}",
        f"- Mật độ xây dựng: {qm.get('mat_do_xay_dung_pct')}%",
        f"- Đất công trình: {qm.get('dat_cong_trinh_m2')} m2; cây xanh {qm.get('dien_tich_cay_xanh_m2')} m2",
        f"- Số tầng hầm: {qm.get('so_tang_ham')}; tổng diện tích sàn hầm {qm.get('tong_dien_tich_san_ham_m2')} m2",
        f"- Cấp công trình: {qm.get('cap_cong_trinh')}",
        "- Tòa D (The Maris Soleil - Wyndham): "
        f"{toa_d.get('so_tang_noi')} tầng / {toa_d.get('chieu_cao_m')} m / "
        f"{toa_d.get('tong_san_pham')} sản phẩm (còn {toa_d.get('san_pham_con_lai_catalog')} theo catalog). "
        f"Tình trạng: {toa_d.get('tinh_trang')}",
        "- Tòa A1 (The Grand Soleil - Trademark Collection): "
        f"{toa_a1.get('so_tang_noi')} tầng / {toa_a1.get('chieu_cao_m')} m / "
        f"{toa_a1.get('tong_san_pham')} sản phẩm (còn {toa_a1.get('san_pham_con_lai_catalog')} theo catalog). "
        f"Tình trạng: {toa_a1.get('tinh_trang')}",
        f"- Tổng sản phẩm còn lại theo catalog: {qm.get('tong_san_pham_con_lai_catalog')}",
        f"- Lưu ý: {qm.get('note_san_pham')}",
    ]

    def _co_cau(tower_key: str) -> str:
        tower = data.get("co_cau_can_ho", {}).get(tower_key, {})
        lines = [f"- {loai}: {info.get('so_can')} căn, diện tích {info.get('dien_tich_m2')} m2"
                 for loai, info in tower.items() if loai != "tong_can"]
        return "\n".join(lines) if lines else "- (không có dữ liệu)"

    co_cau = [
        f"Tòa A1 (tổng {data.get('co_cau_can_ho', {}).get('toa_A1', {}).get('tong_can')} căn):",
        _co_cau("toa_A1"),
        f"Tòa D (tổng {data.get('co_cau_can_ho', {}).get('toa_D', {}).get('tong_can')} căn):",
        _co_cau("toa_D"),
        f"- Ghi chú: {data.get('co_cau_can_ho', {}).get('note')}",
    ]

    pl = data.get("phap_ly", {})
    phap_ly = [
        "- Quyết định chủ trương đầu tư: " + str(pl.get("quyet_dinh_chu_truong")),
        "- Quyết định 6608: " + str(pl.get("quyet_dinh_6608")),
        "- GCN PCCC: " + str(pl.get("gcn_pccc")),
        "- Sở hữu: " + str(pl.get("so_huu")),
        "- Ranh giới: " + str(pl.get("ranh_gioi")),
        "- Đối tượng giao dịch: " + str(pl.get("doi_tuong_giao_dich")),
        "- Sổ đỏ: " + str(pl.get("so_do")),
        "- Bảo hành: " + str(pl.get("bao_hanh")),
        "- Kinh phí bảo trì: " + str(pl.get("kinh_phi_bao_tri")),
        "- Chuyển nhượng: " + str(pl.get("chuyen_nhuong")),
        "- Gia hạn bàn giao: " + str(pl.get("gia_han_ban_giao")),
    ]

    ha_tang = [f"- {k}: {v}" for k, v in data.get("ha_tang", {}).items()]
    ha_tang.extend(f"- {k}: {v}" for k, v in data.get("phi_dich_vu", {}).items())

    sections = [
        ("Thông tin chung", "\n".join(o for o in overview if o)),
        ("Quy mô dự án", "\n".join(quy_mo)),
        ("Cơ cấu căn hộ", "\n".join(co_cau)),
        ("Tiện ích", str(data.get("tien_ich"))),
        ("Bàn giao nội thất", str(data.get("ban_giao_noi_that"))),
        ("Hạ tầng kỹ thuật và dịch vụ", "\n".join(ha_tang)),
        ("Pháp lý và sở hữu", "\n".join(phap_ly)),
        ("Ngân hàng HTLS", "\n".join(f"- {b}" for b in data.get("ngan_hang_htls", []))),
        ("Liên hệ", "\n".join(f"- {k}: {v}" for k, v in data.get("lien_he", {}).items())),
    ]
    metadata = {
        "project_name": data.get("ten_thuong_mai"),
        "location": data.get("vi_tri"),
        "developer": data.get("chu_dau_tu"),
        "total_units": qm.get("tong_san_pham_con_lai_catalog"),
        "handover": "Tòa D: 07/2025; Tòa A1: Q4/2027",
        "trust": "estimate",
    }
    return DocumentDef(
        doc_id="project-soleil-2026q3",
        kind="project",
        title="Hồ sơ tổng quan dự án The Soleil Đà Nẵng",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


def _qna_doc() -> DocumentDef:
    """Render the QnA PDF OCR as cleaned text (table rows too noisy to split per Q)."""
    source = _OCR_DIR / "2025-10-30-bo-cau-hoi-ban-hang-da-the-soleil-da-nang.md"
    return DocumentDef(
        doc_id="project-soleil-qna",
        kind="project",
        title="Bộ câu hỏi bán hàng dự án The Soleil Đà Nẵng (QnA)",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata={
            "project_name": "The Soleil Đà Nẵng",
            "origin": "qna_ocr",
            "source": "data/_processed/soleil/_extract/ocr/2025-10-30-bo-cau-hoi-ban-hang-da-the-soleil-da-nang.md",
            "trust": "estimate",
        },
        sections=[(None, _read_ocr("2025-10-30-bo-cau-hoi-ban-hang-da-the-soleil-da-nang.md"))],
    )


# price docs - matrix + payment schedule + discount policy

def _price_matrix_doc() -> DocumentDef:
    """Event-basket price sheet: 9 unit types (block x type) with min-max VND."""
    data = _load_json("price_matrix.json")
    source = _DATA_DIR / "price_matrix.json"
    note = data.get("note", "")
    warning = data.get("price_per_m2_warning", "")

    sections: list[tuple[str | None, str]] = [
        (
            "Ghi chú chung",
            f"- Đơn vị: {data.get('unit')}\n- {note}\n- {warning}",
        )
    ]
    for t in data.get("types", []):
        gia = t.get("gia", {})
        body = [
            f"- Loại căn: {t['loai']}",
            f"- Tòa: {t.get('ten_toa')} (block {t.get('block')})",
            f"- Mã căn mẫu: {', '.join(t.get('ma_can_mau', []))}",
            f"- Diện tích thông thủy: {t.get('dien_tich_thong_thuy_m2')} m2",
            f"- Giá tổng: {gia.get('tong_vnd')} VND ({gia.get('ty')} tỷ đồng)",
            f"- Đơn giá m2: {t.get('gia_m2_ty')} tỷ đồng/m2",
        ]
        sections.append((f"Giá loại căn {t['loai']} - {t.get('ten_toa')}", "\n".join(body)))

    metadata = dict(_PRICE_META)
    metadata.update({
        "price_structure": "event-basket",
        "source": source.relative_to(_ROOT).as_posix(),
    })
    return DocumentDef(
        doc_id="price-soleil-2026q3",
        kind="price",
        title="Bảng giá sự kiện The Soleil Đà Nẵng Q3/2026 (event basket)",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


def _payment_doc() -> DocumentDef:
    """Payment methods + booking/deposit + 17-milestone schedule."""
    data = _load_json("payment_methods.json")
    source = _DATA_DIR / "payment_methods.json"
    sections: list[tuple[str | None, str]] = []

    bd = data.get("booking_and_deposit", {})
    ck = data.get("chuyen_khoan", {})
    sections.append(
        (
            "Booking và tiền cọc",
            "\n".join(
                f"- {k}: {v}"
                for k, v in {
                    "Tiền cọc": bd.get("deposit_vnd"),
                    "Ghi chú cọc": bd.get("deposit_note"),
                }.items()
                if v is not None
            ),
        )
    )
    sections.append(
        (
            "Chuyển khoản",
            "\n".join(
                f"- {k}: {v}" for k, v in {
                    "Chủ tài khoản": ck.get("chu_tai_khoan"),
                    "Số tài khoản": ck.get("so_tai_khoan"),
                    "Ngân hàng": ck.get("ngan_hang"),
                    "Cú pháp": ck.get("cu_phap"),
                }.items()
                if v is not None
            ),
        )
    )

    for m in data.get("methods", []):
        body = [
            f"- Chiết khấu phương thức: {m.get('ck_phuong_thuc_pct')}",
            f"- Khuyến mại: {m.get('ck_khuyen_mai_pct')}",
            f"- Tổng chiết khấu: {m.get('tong_ck_pct')}",
        ]
        if m.get("only_block"):
            body.append(f"- Chỉ áp dụng tòa: {m['only_block']}")
        for ms in m.get("milestones", []):
            pct = f"{ms['pct']}%" if ms.get("pct") is not None else ms.get("amount", "-")
            extra = f" ({ms['note']})" if ms.get("note") else ""
            body.append(f"  {ms.get('order', '?')}. {ms.get('milestone')} — {pct}{extra}")
        sections.append((f"Phương thức: {m.get('name')}", "\n".join(body)))

    metadata = dict(_PRICE_META)
    metadata.update({
        "price_structure": "payment-schedule",
        "source": source.relative_to(_ROOT).as_posix(),
    })
    return DocumentDef(
        doc_id="price-soleil-2026q3-payment",
        kind="price",
        title="Lịch thanh toán và phương thức chiết khấu — The Soleil Đà Nẵng Q3/2026",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


def _policy_doc() -> DocumentDef:
    """Sales policy: discount matrix per tower + early booking + deposit + HTLS."""
    data = _load_json("business_rules.json")
    source = _DATA_DIR / "business_rules.json"
    rules = data.get("rules", {})
    sections: list[tuple[str | None, str]] = []

    dm = rules.get("discount_matrix", {})
    for tower in ("A1", "D"):
        entries = [
            f"- {method}: CK phương thức {info.get('ck_phuong_thuc')}%, "
            f"khuyến mại {info.get('khuyen_mai')}%, tổng {info.get('tong')}% — "
            f"{info.get('note', '')}"
            for method, info in dm.get(tower, {}).items()
        ]
        if entries:
            sections.append((f"Ma trận chiết khấu Tòa {tower}", "\n".join(entries)))

    eb = rules.get("early_booking", {})
    sections.append(("Khuyến mại chung / early booking", f"- {eb.get('value')}\n- {eb.get('deadline')}"))

    dp = rules.get("deposit", {})
    sections.append(("Tiền cọc (TTĐC)", f"- {dp.get('total_vnd')} đ — {dp.get('note')}\n- {dp.get('base_rule')}"))

    htls = rules.get("htls", {})
    htls_lines = [f"- Ngân hàng: {', '.join(htls.get('banks', []))}"]
    for tower in ("A1", "D"):
        t = htls.get(tower, {})
        if t:
            htls_lines.append(
                f"- Tòa {tower}: vay tối đa {t.get('vay_max_pct')}%, HTLS tối đa "
                f"{t.get('htls_max_pct')}%, lãi {t.get('interest_pct')}%, "
                f"thời hạn {t.get('term_months')} tháng — {t.get('note', '')}"
            )
    mn = htls.get("mua_nha_0_dong", {})
    if mn:
        htls_lines.append(
            f"- Mua nhà 0 đồng (chỉ A1): vay {mn.get('vay_max_pct')}%, HTLS "
            f"{mn.get('htls_max_pct')}%, thời hạn {mn.get('term_months')} tháng — {mn.get('note', '')}"
        )
    htls_lines.append(f"- Ân hạn: {htls.get('grace_note')}")
    sections.append(("Chính sách HTLS", "\n".join(htls_lines)))

    for key in ("uy_thac_cho_thue", "vip", "kpbt", "thanh_toan_som", "sale_floor", "price_quality"):
        item = rules.get(key, {})
        if item:
            sections.append((item.get("label", key), f"- {item.get('value')}\n- {item.get('note', '')}"))

    metadata = dict(_PRICE_META)
    metadata.update({
        "price_structure": "discount-policy",
        "source": source.relative_to(_ROOT).as_posix(),
    })
    return DocumentDef(
        doc_id="price-soleil-2026q3-policy",
        kind="price",
        title="Chính sách bán hàng và hỗ trợ tài chính — The Soleil Đà Nẵng Q3/2026",
        source_path=source,
        effective_from=CONFIRMED_DATE,
        effective_to=None,
        metadata=metadata,
        sections=sections,
    )


# legal docs - investment decision + master plan + PCCC certificate

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
        DocumentDef(
            doc_id="legal-soleil-chu-truong-2018",
            kind="legal",
            title="Quyết định chủ trương đầu tư Tổ hợp Ánh Dương - Soleil (11/09/2018)",
            source_path=_OCR_DIR / "2018-09-11-soleil-chu-truong-au-tu.md",
            effective_from=_dt.date(2018, 9, 11),
            effective_to=None,
            metadata=_legal_metadata(
                "5753/QĐ-UBND (thay thế) / QĐ 11/09/2018", "quyet-dinh",
                "UBND TP Đà Nẵng", "2018-09-11",
                ["chấp thuận chủ trương đầu tư", "chấp thuận nhà đầu tư",
                 "PPC An Thịnh Đà Nẵng", "50 năm từ 13/10/2017"],
                ["legal-soleil-qd6608-2016", "legal-soleil-pccc-2017"],
            ),
            sections=[(None, _read_ocr("2018-09-11-soleil-chu-truong-au-tu.md"))],
        ),
        DocumentDef(
            doc_id="legal-soleil-qd6608-2016",
            kind="legal",
            title="Quyết định 6608/QĐ-UBND phê duyệt tổng mặt bằng 1/500 (28/09/2016)",
            source_path=_OCR_DIR / "q-6608-phe-duyet-tong-mat-bang.md",
            effective_from=_dt.date(2016, 9, 28),
            effective_to=None,
            metadata=_legal_metadata(
                "6608/QĐ-UBND", "quyet-dinh", "UBND TP Đà Nẵng", "2016-09-28",
                ["quy hoạch tổng mặt bằng", "1/500", "hệ số SDĐ 16,83"],
                ["legal-soleil-chu-truong-2018"],
            ),
            sections=[(None, _read_ocr("q-6608-phe-duyet-tong-mat-bang.md"))],
        ),
        DocumentDef(
            doc_id="legal-soleil-pccc-2017",
            kind="legal",
            title="Giấy chứng nhận thẩm duyệt thiết kế PCCC số 4602/TD-PCCC-P6 (18/08/2017)",
            source_path=_OCR_DIR / "gcn-4602-pccc-chung-nhan-pccc-gd-2.md",
            effective_from=_dt.date(2017, 8, 18),
            effective_to=None,
            metadata=_legal_metadata(
                "4602/TD-PCCC-P6", "gcn-pccc", "Cục Cảnh sát PCCC & CNCH, Bộ Công an",
                "2017-08-18",
                ["phòng cháy chữa cháy", "thẩm duyệt thiết kế", "giai đoạn 2"],
                ["legal-soleil-chu-truong-2018"],
            ),
            sections=[(None, _read_ocr("gcn-4602-pccc-chung-nhan-pccc-gd-2.md"))],
        ),
    ]


# public API (builder entry points)

def build_documents() -> list[ParsedDoc]:
    """Build every Soleil registry document from the confirmed processed corpus."""
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


def main() -> int:
    """CLI: build + validate the registry. `--json` emits the registry as JSON lines."""
    import argparse

    ap = argparse.ArgumentParser(description="Build + validate Soleil document registry.")
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
    print("VALIDATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
