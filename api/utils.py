"""Small pure helpers shared across the api package."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone


def sha256_hex(text: str | None) -> str:
    """SHA-256 hex digest of UTF-8 normalized text (for prompt/answer hashing)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 string (audit timestamps)."""
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default: float = 0.0) -> float:
    """Parse a value as float, returning default on None or invalid input."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def truncate_str(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending an ellipsis when cut."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def slugify(text: str) -> str:
    """ASCII-slug of a string ('Căn hộ 2PN' -> 'can-ho-2pn'), for ids/labels."""
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
