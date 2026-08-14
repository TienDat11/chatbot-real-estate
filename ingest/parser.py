"""Parser — Docling extracts text and tables, pre-chunked by article/clause up to the cap.

(plan §3.2 step 1 + §3.7 A1) Pre-chunk by article/clause with a hard cap of chunk_cap chars;
long articles split on clause boundaries. MinerU is an optional fallback for poor PDF scans.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from dataclasses import dataclass, field

from ingest.config import settings


class ParserError(RuntimeError):
    """File could not be parsed — the caller decides whether to route it to manual review."""


@dataclass
class ParsedSection:
    """One pre-chunk: text plus optional section title and tables."""
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
    # Half-open interval [effective_from, effective_to); None to = open-ended
    # (schema.sql documents). Defaults mirror load_document: today / NULL.
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None
    # Per-kind attributes (docs/data-contract.md); top-level columns must NOT be duplicated here.
    metadata: dict = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.sections)


# Matches article headers in Vietnamese legal text: "Điều 123.", "Điều 1.", "Điều 45:" ...
_ARTICLE_RE = re.compile(
    r"^\s*Điều\s+(\d+|[IVXLCDM]+)\s*[.:]\s*(\S.*?)$",
    re.IGNORECASE | re.MULTILINE,
)
# Matches clause numbers starting a line within an article, e.g. "1. ", "2. ".
_CLAUSE_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+", re.MULTILINE)


def _split_articles(text: str) -> list[tuple[str, str]]:
    """Return (article_title, body) pairs; falls back to a single raw chunk when no articles match."""
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
    """Split clauses within an article body; oversized clauses are still hard-cut at cap."""
    if len(body) <= cap:
        return [body]
    clauses = _CLAUSE_RE.split(body)
    # _CLAUSE_RE.split prepends numbers — reassemble as (text, num, text, num, ...).
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
        # Hard-cut when still oversized.
        while len(buf) > cap:
            chunks.append(buf[:cap])
            buf = buf[cap:]
        chunks.append(buf.strip())
    return chunks or [body]


def _prechunk(article_title: str | None, body: str, cap: int) -> list[ParsedSection]:
    """Article to one section when short, or multiple clause-based sections otherwise."""
    if len(body) <= cap:
        return [ParsedSection(text=body, section_title=article_title)]
    return [
        ParsedSection(text=piece, section_title=article_title)
        for piece in _split_clauses(body, cap)
    ]


async def parse_document(path: str, kind: str, doc_id: str | None = None, title: str | None = None) -> ParsedDoc:
    """Parse a PDF/Word/MD/JSON file with Docling into pre-chunked sections.

    Raises:
        ParserError: file is unreadable or no text could be extracted.
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
        # Poor PDF scan — hint at the optional MinerU fallback.
        raise ParserError(f"Docling parse thất bại ({path}): {exc}") from exc

    if not raw_text.strip():
        raise ParserError(f"Không extract được text từ: {path}")

    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    sections: list[ParsedSection] = []
    for article_title, body in _split_articles(raw_text):
        sections.extend(_prechunk(article_title, body, settings.chunk_cap))

    if not sections:
        sections = [ParsedSection(text=raw_text[: settings.chunk_cap])]

    # Derive doc_id from the filename as a fallback.
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
