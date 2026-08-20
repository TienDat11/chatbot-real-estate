"""Sales synonym enrichment (Story 4.4, R7).

Maps colloquial phrasings used by older buyers (45-70) to canonical sales
keywords, so the RAG hybrid retrieval gets the right hl_keywords even when the
query is informal ("nhà 2 ngủ", "hướng biển", "trả góp 0%", "thảnh thơi"...).

Enrichment is ADD-ONLY: it appends canonical keywords to the LLM's hl_keywords
and never replaces or rewrites the LLM output. Pure and synchronous.
"""

from __future__ import annotations

import re

# Colloquial -> canonical keyword(s). Keys are matched case-insensitively; values
# are the canonical hl_keywords that RAG should receive. Order within a tuple is
# not significant. ASCII abbreviation keys ("ck", "eb", "htls", "studio") match on
# word boundaries so they don't fire inside unrelated words ("check", "web");
# Vietnamese keys ("nhà 2 ngủ", "biển", "cọc") match as substrings.
SALES_SYNONYMS: dict[str, tuple[str, ...]] = {
    # bedrooms / unit type
    "nhà 2 ngủ": ("2PN",),
    "2 phòng ngủ": ("2PN",),
    "nhà 1 ngủ": ("1PN",),
    "1 phòng ngủ": ("1PN",),
    "nhà 3 ngủ": ("3PN",),
    "3 phòng ngủ": ("3PN",),
    "studio": ("Studio",),
    "1 ngủ rưỡi": ("1.5PN",),
    "1pn+": ("1.5PN",),
    # view / position
    "hướng biển": ("view biển",),
    "biển": ("view biển",),
    "nội khu": ("nội khu",),
    "căn góc": ("căn góc",),
    "góc": ("căn góc",),
    # payment / financing
    "trả chậm": ("HTLS", "0%"),
    "trả góp 0%": ("HTLS", "0%"),
    "vay 0%": ("HTLS", "0%"),
    "htls": ("HTLS", "0%"),
    "đóng sớm": ("sớm 95",),
    "sớm 95": ("sớm 95",),
    "som95": ("sớm 95",),
    "thảnh thơi": ("thanh toán thảnh thơi",),
    "giảm giá": ("chiết khấu",),
    "chiết khấu": ("chiết khấu",),
    "ck": ("chiết khấu",),
    "early booking": ("early booking",),
    "eb": ("early booking",),
    "cọc": ("tiền cọc",),
    "bàn giao": ("bàn giao",),
}


def _matches(key: str, q: str) -> bool:
    """Match a synonym key against the lowercased query.

    ASCII-only keys (abbreviations like "ck", "eb", "htls", "studio") match on
    word boundaries so they don't fire inside unrelated words ("check", "web").
    Vietnamese keys match as substrings: Python's \\b treats diacritics as
    non-word, so a boundary match would silently miss "cọc" / "biển".
    """
    if key.isascii():
        return re.search(rf"(?<!\w){re.escape(key)}(?!\w)", q) is not None
    return key in q


def enrich_hl_keywords(query: str, hl_keywords: list[str]) -> list[str]:
    """Append canonical synonyms to hl_keywords; ADD-only, deduplicated.

    Returns a new list: the caller's keywords first (order preserved), then any
    canonical keyword whose colloquial key appears in the query and is not
    already present. Never mutates the input list.
    """
    out: list[str] = list(hl_keywords or [])
    q = (query or "").lower()
    for colloquial, canonicals in SALES_SYNONYMS.items():
        if _matches(colloquial, q):
            for c in canonicals:
                if c not in out:
                    out.append(c)
    return out


__all__ = ["SALES_SYNONYMS", "enrich_hl_keywords"]
