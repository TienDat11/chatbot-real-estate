"""Batch OCR for the essential Soleil PDFs (project legal + policy + QnA).

Runs docling with torch dynamo disabled (Windows has no MSVC `cl` compiler, which
the RT-DETR layout model needs when torch.compile is on). Writes one markdown
extract per PDF into data/_processed/soleil/_extract/ocr/<slug>.md.

The two huge scan-only certifications (Nghiệm thu PCCC 32MB, Nghiệm thu hoàn
thành 12MB) are intentionally skipped here — they are scanned-in legal stamps
with no structured Q&A value; they remain addressable later.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import unicodedata

os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

from docling.document_converter import DocumentConverter  # noqa: E402

DATA = pathlib.Path(r"D:/chatbot-real-estate/data/soleil")
OUT = pathlib.Path(r"D:/chatbot-real-estate/data/_processed/soleil/_extract/ocr")
OUT.mkdir(parents=True, exist_ok=True)


def slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


TARGETS = [
    "2018.09.11 - Soleil - Chủ trương đầu tư.pdf",
    "QĐ 6608 - Phê duyệt tổng mặt bằng.PDF",
    "2025.10.30 - BỘ CÂU HỎI BÁN HÀNG - DA THE SOLEIL DA NANG.pdf",
    "05.2026 - CHINH  SACH BAN HANG  - A1 - Ban hành.pdf",
    "05.2026- CHINH  SACH BAN HANG  - D - Ban hành.pdf",
    "05.2026 - CS DAC BIET A1 - Ban hành.pdf",
    "05.2026 - CHINH SACH DAC BIET TOA  D - Ban hành.pdf",
    "GCN 4602_PCCC_Chứng nhận PCCC (GD 2).pdf",
    "Soleil - Tiêu chuẩn bàn giao.pdf",
]


def main() -> int:
    conv = DocumentConverter()
    results = {}
    for name in TARGETS:
        path = DATA / name
        if not path.exists():
            print(f"SKIP (missing): {name}")
            results[name] = {"status": "missing"}
            continue
        try:
            res = conv.convert(path)
            md = res.document.export_to_markdown() or ""
            out = OUT / f"{slug(path.stem)}.md"
            out.write_text(md, encoding="utf-8")
            results[name] = {"status": "ok", "chars": len(md), "out": str(out)}
            print(f"OK   {len(md):6} chars  {name}")
        except Exception as exc:  # noqa: BLE001
            results[name] = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            print(f"ERR  {name}: {results[name]['error']}")
    (OUT / "_ocr_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
