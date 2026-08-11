"""Parser — Docling (chính) tách text + bảng, pre-chunk theo Điều/Khoản ≤ cap.

Plan §3.2 step 1 + §3.7 (A1): pre-chunk Điều/Khoản; hard cap ≤ chunk_cap chars;
Điều dài tách theo Khoản. MinerU = fallback cho PDF scan xấu (spike optional).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ingest.config import settings


class ParserError(RuntimeError):
    """Không parse được file — gọi nơi khác quyết định review thủ công."""


@dataclass
class ParsedSection:
    """1 pre-chunk: text + section title + tables (nếu có)."""
    text: str
    section_title: str | None = None
    tables: list[list[list[str]]] = field(default_factory=list)


@dataclass
class ParsedDoc:
    doc_id: str
    title: str
    kind: str  # legal | price | project
    source_file: str
    sections: list[ParsedSection]
    content_hash: str

    @property
    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.sections)


# Regex tách Điều trong văn bản pháp luật VN: "Điều 123.", "Điều 1.", "Điều 45:" ...
_ARTICLE_RE = re.compile(
    r"^\s*Điều\s+(\d+|[IVXLCDM]+)\s*[.:]\s*(\S.*?)$",
    re.IGNORECASE | re.MULTILINE,
)
# Regex tách Khoản: "1. ", "2. " ở đầu dòng (trong phạm vi Điều)
_CLAUSE_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+", re.MULTILINE)


def _split_articles(text: str) -> list[tuple[str, str]]:
    """Trả list (article_title, body). Nếu không nhận diện được Điều → 1 chunk thô."""
    matches = list(_ARTICLE_RE.finditer(text))
    if not matches:
        return [(None, text)]

    parts: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts.append((m.group(0).strip(), text[start:end].strip()))
    return parts


def _split_clauses(body: str, cap: int) -> list[str]:
    """Tách Khoản trong body; các Khoản dài vẫn cắt cứng theo cap."""
    if len(body) <= cap:
        return [body]
    clauses = _CLAUSE_RE.split(body)
    # _CLAUSE_RE.split chèn số vào đầu — gộp lại dạng (text, num, text, num, ...)
    chunks: list[str] = []
    buf = clauses[0] if clauses else ""
    for i in range(1, len(clauses) - 1, 2):
        num, seg = clauses[i], clauses[i + 1]
        piece = f"{num}. {seg}".strip()
        if len(buf) + len(piece) > cap and buf:
            chunks.append(buf.strip())
            buf = piece
        else:
            buf = f"{buf}\n{piece}".strip()
    if buf:
        # cắt cứng nếu siêu dài
        while len(buf) > cap:
            chunks.append(buf[:cap])
            buf = buf[cap:]
        chunks.append(buf.strip())
    return chunks or [body]


def _prechunk(article_title: str | None, body: str, cap: int) -> list[ParsedSection]:
    """article → 1 ParsedSection/body (đủ ngắn) hoặc nhiều section theo Khoản."""
    if len(body) <= cap:
        return [ParsedSection(text=body, section_title=article_title)]
    return [
        ParsedSection(text=piece, section_title=article_title)
        for piece in _split_clauses(body, cap)
    ]


async def parse_document(path: str, kind: str, doc_id: str | None = None, title: str | None = None) -> ParsedDoc:
    """Parse file (PDF/Word/MD/JSON) bằng Docling → ParsedDoc với pre-chunks.

    Raises:
        ParserError: file không đọc được / không extract được text.
    """
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:  # pragma: no cover
        raise ParserError(
            "Thiếu docling (pip install -r requirements.txt). "
            "MinerU fallback chưa bật — spike optional."
        ) from exc

    import pathlib

    p = pathlib.Path(path)
    if not p.exists():
        raise ParserError(f"File không tồn tại: {path}")

    raw_text = ""
    try:
        converter = DocumentConverter()
        result = converter.convert(p)
        raw_text = result.document.export_to_markdown() or ""
    except Exception as exc:  # noqa: BLE001
        # PDF scan xấu → nhắc MinerU fallback (spike)
        raise ParserError(f"Docling parse thất bại ({path}): {exc}") from exc

    if not raw_text.strip():
        raise ParserError(f"Không extract được text từ: {path}")

    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    sections: list[ParsedSection] = []
    for article_title, body in _split_articles(raw_text):
        sections.extend(_prechunk(article_title, body, settings.chunk_cap))

    if not sections:
        sections = [ParsedSection(text=raw_text[: settings.chunk_cap])]

    # doc_id fallback từ filename
    if not doc_id:
        stem = p.stem.lower().replace(" ", "-")
        doc_id = f"{kind}-{stem}"
    if not title:
        title = p.stem

    return ParsedDoc(
        doc_id=doc_id,
        title=title,
        kind=kind,
        source_file=str(p),
        sections=sections,
        content_hash=content_hash,
    )
