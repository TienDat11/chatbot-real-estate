"""Gemini-vision OCR CLI — the project-standard replacement for aibox analyze_image.

Transcribes scanned project images/PDFs (floor plans, price tables, payment
schedules, legal scans) into faithful Vietnamese Markdown via the app's own
OpenAI-compatible LLM binding (Settings + OpenAICompatibleLLM, Gemini route).

Credentials come from env/.env only (LLM_API_KEY / LLM_BASE_URL); nothing is
hardcoded or printed. PDFs are rasterized in-memory with pypdfium2 at the
requested DPI (default 300, proven during the QD6608 remediation).

Usage:
    .venv/Scripts/python scripts/ocr_vision.py IMAGE_OR_PDF [MORE...] [options]
    .venv/Scripts/python scripts/ocr_vision.py "data/soleil/QD 6608.PDF" --pages 1-3
    .venv/Scripts/python scripts/ocr_vision.py scan.png --out out/scan.md

Output defaults to ``<input_stem>.ocr.md`` next to the input; existing files are
never overwritten unless --force is passed (protects ingest-used documents).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import sys
import time
from pathlib import Path

# Make `import api` work regardless of the process CWD (script lives in scripts/).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.infrastructure.adapters.openai_compatible_llm import LLMError
from api.infrastructure.config.config import get_settings
from api.infrastructure.dependencies import get_llm

# Faithful-transcription contract: proven verbatim during the QD6608 remediation
# (data/_processed/soleil/_extract/remediation_tmp/ocr_gemini.py). Any edit here
# changes ingestion output, so treat the wording as a locked prompt asset.
_PROMPT = (
    "Ban la cong cu OCR. CHEP LAI NGUYEN VAN toan bo van ban tieng Viet trong anh scan van ban "
    "phap luat nay - KHONG tom tat, KHONG binh luan, KHONG dich, KHONG them bo sung noi dung. "
    "Yeu cau bat buoc:\n"
    "1. Chinh xac 100% dau tieng Viet (chu Viet co dau day du, vi du: QUYẾT ĐỊNH, ĐIỀU, "
    "ỦY BAN NHÂN DÂN, THÀNH PHỐ ĐÀ NẴNG).\n"
    "2. Giu nguyen thu tu, so hieu dieu/khoan/diem, dau gach dau dong neu co.\n"
    "3. So lieu phai dung: dien tich (m2), so tang, tang ham, he so su dung dat, mat do xay dung, "
    "ty le phantram, so van ban trich dan (vi du: So 6608/QD-UBND; cac Thong tu, Nghi dinh, "
    "Quyet dinh duoc trich y).\n"
    "4. Neu co bang, chep lai dang bang markdown voi dung so lieu tung o.\n"
    "5. Cho tu khong doc duoc, ghi [khong ro] thay vi doan.\n"
    "6. Tra ve CHI van ban da chep, khong kem nhan giai thich."
)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

# Payload guard: base64 inflates ~4/3x and gateways reject oversized inline data,
# so anything past this is downscaled to a JPEG capped at _MAX_EDGE px first.
_MAX_INLINE_BYTES = 6 * 1024 * 1024
_MAX_EDGE = 4000  # same clamp the QD6608 rasterizer used to keep pages legible
_ATTEMPTS = 3


def parse_pages(spec: str | None) -> list[int] | None:
    """Turn a 1-based page spec like '1,3,5-7' into an ordered list, or None."""
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            lo_i, hi_i = int(lo), int(hi)  # ValueError propagates to argparse
            if lo_i < 1 or hi_i < lo_i:
                raise argparse.ArgumentTypeError(f"invalid page range: {part!r}")
            pages.update(range(lo_i, hi_i + 1))
        else:
            num = int(part)
            if num < 1:
                raise argparse.ArgumentTypeError(f"page numbers are 1-based: {part!r}")
            pages.add(num)
    if not pages:
        raise argparse.ArgumentTypeError("empty --pages spec")
    return sorted(pages)


def _jpeg_bytes(img, quality: int) -> bytes:
    """Encode a PIL image as RGB JPEG bytes in memory."""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _downscale_to_jpeg(data: bytes, quality: int = 92) -> bytes:
    """Cap an oversized bitmap to _MAX_EDGE px and re-encode as JPEG."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        if max(w, h) > _MAX_EDGE:
            ratio = _MAX_EDGE / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)))
        return _jpeg_bytes(img, quality)


def load_image(path: Path) -> tuple[str, bytes]:
    """Return (mime, bytes) for one image file, downscaling oversized inputs."""
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower())
    if mime is None:
        raise SystemExit(f"ERROR: unsupported image type {path.suffix!r}: {path}")
    data = path.read_bytes()
    if len(data) > _MAX_INLINE_BYTES:
        data = _downscale_to_jpeg(data)
        mime = "image/jpeg"
    return mime, data


def load_pdf_pages(path: Path, pages: list[int] | None, dpi: int) -> list[tuple[int, bytes]]:
    """Rasterize selected PDF pages (1-based) to JPEG bytes at the given DPI."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise SystemExit("ERROR: pypdfium2 not installed in this venv (needed for PDF input)")
    doc = pdfium.PdfDocument(str(path))
    try:
        n = len(doc)
        wanted = pages or list(range(1, n + 1))
        out: list[tuple[int, bytes]] = []
        for num in wanted:
            if num > n:
                raise SystemExit(f"ERROR: {path.name} has {n} page(s); page {num} requested")
            bitmap = doc[num - 1].render(scale=dpi / 72.0)
            out.append((num, _jpeg_from_pil(bitmap.to_pil())))
        return out
    finally:
        doc.close()


def _jpeg_from_pil(img, quality: int = 92) -> bytes:
    """Encode a pypdfium2-rendered PIL page, capping very long edges."""
    w, h = img.size
    if max(w, h) > _MAX_EDGE:
        ratio = _MAX_EDGE / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)))
    return _jpeg_bytes(img, quality)


async def transcribe(llm, label: str, mime: str, data: bytes, *, model: str | None,
                     max_tokens: int, timeout: float) -> str:
    """One vision completion with bounded retries; raises LLMError when exhausted."""
    b64 = base64.b64encode(data).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ],
    }]
    last_exc: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return await llm.complete(
                messages, model=model, max_tokens=max_tokens, timeout=timeout
            )
        except LLMError as exc:
            last_exc = exc
            print(f"{label}: attempt {attempt}/{_ATTEMPTS} failed: {exc}", file=sys.stderr)
            if attempt < _ATTEMPTS:
                time.sleep(3 * attempt)
    raise LLMError(f"{label}: all {_ATTEMPTS} attempts failed") from last_exc


def resolve_output(path: Path, out: Path | None, multiple_inputs: bool) -> Path:
    """Default output is <stem>.ocr.md next to the input; --out only for one input."""
    if out is not None:
        if multiple_inputs:
            raise SystemExit("ERROR: --out applies to a single input; drop it or pass one file")
        return out
    return path.parent / f"{path.stem}.ocr.md"


def write_output(out: Path, sections: list[tuple[str, str]], force: bool) -> None:
    """Write the Markdown transcript, refusing to clobber existing files."""
    if out.exists() and not force:
        raise SystemExit(
            f"ERROR: refusing to overwrite {out} (use --force if you are sure)"
        )
    multi = len(sections) > 1
    parts: list[str] = []
    for label, text in sections:
        parts.append(f"## {label}\n\n{text.strip()}\n" if multi else text.strip() + "\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(parts), encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    llm = get_llm()  # fails closed when LLM_API_KEY / LLM_BASE_URL are unset
    model = args.model  # None -> adapter default (settings.llm_model_answer, gemini-3.6-flash)
    failures = 0

    for path in args.inputs:
        path = path.resolve()
        if not path.is_file():
            print(f"ERROR: not a file: {path}", file=sys.stderr)
            failures += 1
            continue
        out = resolve_output(path, args.out, len(args.inputs) > 1)
        sections: list[tuple[str, str]] = []

        if path.suffix.lower() == ".pdf":
            if args.pages and len(args.inputs) > 1:
                print("note: --pages applies to PDF inputs only (ignored for others)", file=sys.stderr)
            for num, data in load_pdf_pages(path, args.pages, args.dpi):
                label = f"{path.name}#p{num}"
                try:
                    text = await transcribe(
                        llm, label, "image/jpeg", data,
                        model=model, max_tokens=args.max_tokens, timeout=args.timeout,
                    )
                except LLMError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    failures += 1
                    continue
                sections.append((f"Page {num}", text))
                print(f"{label}: ok {len(text)} chars")
        else:
            mime, data = load_image(path)
            text = await transcribe(
                llm, path.name, mime, data,
                model=model, max_tokens=args.max_tokens, timeout=args.timeout,
            )
            sections.append((path.name, text))
            print(f"{path.name}: ok {len(text)} chars")

        if sections:
            write_output(out, sections, args.force)
            print(f"-> {out} ({len(sections)} page(s))")
    return 1 if failures else 0


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="ocr_vision",
        description=(
            "Faithful Vietnamese OCR of project images/PDFs via the app's Gemini "
            "vision binding (OpenAI-compatible). Replaces the retired aibox "
            "analyze_image path. Credentials: LLM_API_KEY/LLM_BASE_URL from env/.env."
        ),
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="image file(s) or PDF file(s)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output .md path (single input only; default <stem>.ocr.md next to input)")
    parser.add_argument("--pages", type=parse_pages, default=None,
                        help="1-based PDF page subset, e.g. '1,3,5-7' (ignored for images)")
    parser.add_argument("--dpi", type=int, default=300,
                        help="PDF rasterization DPI (default 300; 72-600 sane range)")
    parser.add_argument("--model", default=None,
                        help="vision model id (default: settings.llm_model_answer, i.e. gemini-3.6-flash)")
    parser.add_argument("--max-tokens", type=int, default=16384,
                        help="max output tokens per page (default 16384)")
    parser.add_argument("--timeout", type=float, default=240.0,
                        help="per-page LLM timeout in seconds (default 240)")
    parser.add_argument("--force", action="store_true",
                        help="allow overwriting an existing output file")
    args = parser.parse_args()

    if not (72 <= args.dpi <= 1200):
        parser.error("--dpi must be within 72..1200")
    if not args.inputs:
        parser.error("at least one input file is required")

    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
