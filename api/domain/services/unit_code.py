"""Pure helpers for parsing and canonicalizing concrete apartment unit codes."""

from __future__ import annotations

import re
import unicodedata

_UNIT_CODE_RE = re.compile(r"\bCH-\d{1,2}[A-Z]?\b", re.IGNORECASE)
_PRICE_QUERY_RE = re.compile(
    r"\bgia\b|\bprice\b|\bbao nhieu tien\b|\bbao nhieu (?:dong|d\s+ong)\b|\bbang gia\b|\b(?:gia|price)\s+(?:dong|d\s+ong|vnd|trieu|ty)\b",
    re.IGNORECASE,
)


def normalize_unit_code(code: str) -> str | None:
    """Return the canonical zero-padded uppercase unit code, or None if invalid."""
    match = re.fullmatch(r"CH-(\d{1,2})([A-Z]?)", code.strip().upper())
    if not match:
        return None
    number, suffix = match.groups()
    return f"CH-{int(number):02d}{suffix}"


_SUBJECT_SUFFIX_RE = re.compile(r"CH-?(\d{1,2})([A-Z]?)")


def unit_code_from_subject_suffix(suffix: str) -> str | None:
    """Decode the persisted subject-key suffix (dots/dashes dropped by the subject-key normalizer) back to the user-facing code."""
    match = _SUBJECT_SUFFIX_RE.fullmatch((suffix or "").strip().upper())
    if not match:
        return None
    number, letter = match.groups()
    return f"CH-{int(number):02d}{letter}"


def extract_unit_codes(query_text: str) -> list[str]:
    """Return concrete unit codes in canonical form, preserving query order."""
    return [
        normalized
        for match in _UNIT_CODE_RE.findall(query_text or "")
        if (normalized := normalize_unit_code(match)) is not None
    ]

def is_unit_price_query(query_text: str) -> bool:
    """Return whether original query text explicitly asks about money prices."""
    normalized = " ".join(
        unicodedata.normalize("NFKD", query_text or "")
        .replace("đ", "d")
        .replace("Đ", "D")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .split()
    )
    normalized = re.sub(r"\bgia tri\b", "", normalized)
    normalized = re.sub(r"\bty le\b", "", normalized)
    return bool(_PRICE_QUERY_RE.search(normalized))
