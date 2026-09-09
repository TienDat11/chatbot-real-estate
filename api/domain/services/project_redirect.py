"""Cross-project redirect detection (project-awareness guardrail).

Deterministic keyword layer that runs BEFORE RAG retrieval in the conversation
workflow: when the question names a DIFFERENT known project than the session's,
the current project's corpus must never ground the answer (user-reported bug:
standing in Camellia, "Bạn biết gì về soleil không?" was answered from Camellia
data). Detection returns the foreign project so the caller replies with a
guidance message plus a structured ``project_redirect`` signal instead of
running retrieval.

Pure and synchronous — normalization + substring matching only, no LLM, no
I/O — so it is fully unit-testable and cannot fail at runtime.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

_WS_RUN = re.compile(r"\s+")
_WORD_RUN = re.compile(r"[a-z0-9]+")

# Trailing city/province tokens stripped from picker/button labels ('The Soleil
# Đà Nẵng' -> 'The Soleil'). Each token matches its ASCII-folded spelling too,
# because seeds write names without diacritics ('The Camellia Son Tra - Da
# Nang'). A separator (space or dash run) is required before the token so a
# brand word is never eaten. Longest alternative first: 'TP. Hồ Chí Minh' must
# win over bare 'Hồ Chí Minh', else a dangling 'TP.' would survive the strip.
_CITY_TAIL_RE = re.compile(
    "(?:[-–—]|\\s)+(?:"
    "tp\\.?\\s*hồ chí minh|tp\\.?\\s*ho chi minh|"
    "hồ chí minh|ho chi minh|"
    "đà nẵng|da nang|"
    "hà nội|ha noi"
    ")\\s*$",
    re.IGNORECASE,
)

# Friendly Vietnamese guidance replacing retrieval on a redirect turn. The
# project appears twice by design: named once with the full display label,
# then repeated as the compact short name inside the switch instruction
# (matches the assistant's existing tone).
REDIRECT_MESSAGE_TEMPLATE = (
    "Câu hỏi của bạn thuộc dự án {display_name}. Để được tư vấn chính xác, "
    "vui lòng chuyển sang dự án {short_name} để em giải đáp chi tiết nhé ạ."
)


def normalize_project_text(text: str | None) -> str:
    """Lowercase folded form for matching: diacritics stripped, đ->d, spaces collapsed."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WS_RUN.sub(" ", stripped.replace("đ", "d")).strip()


def short_display_name(ten_thuong_mai: str | None) -> str:
    """Human label without the parenthetical qualifier tail.

    'The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite
    Collection)' -> 'The Soleil Đà Nẵng'. The registry's ten_thuong_mai carries
    a long marketing qualifier that would bloat the picker label and the
    redirect button; a name with no parenthetical passes through unchanged.
    """
    if not ten_thuong_mai:
        return ""
    return ten_thuong_mai.split(" (", 1)[0].strip()


def strip_city_suffix(display_name: str | None) -> str:
    """Display label with a trailing city/province token removed.

    'The Soleil Đà Nẵng' -> 'The Soleil'; 'The Camellia Son Tra - Da Nang' ->
    'The Camellia Son Tra' (ASCII seed spelling, dash separator). Supported
    tokens: Đà Nẵng, Hà Nội, TP. Hồ Chí Minh, Hồ Chí Minh — diacritic and
    plain spellings. A name carrying no trailing token passes through
    unchanged; stripping everything (the token WAS the name) falls back to
    the full label so the result is never empty.
    """
    if not display_name:
        return ""
    text = display_name.strip()
    stripped = _CITY_TAIL_RE.sub("", text).strip(" -–—")
    return stripped or text


def project_match_variants(project_key: str | None, display_name: str | None) -> tuple[str, ...]:
    """Normalized needles one project answers to, most specific first.

    Variants (task examples 'the soleil', 'soleil', 'camellia'): the project
    key itself, the full normalized commercial name, the name up to the brand
    token ('the soleil'), and the bare brand token ('soleil' — first
    significant word after a leading 'The').
    """
    variants: list[str] = []

    def _add(candidate: str) -> None:
        if candidate and candidate not in variants:
            variants.append(candidate)

    _add(normalize_project_text(project_key))
    short = normalize_project_text(short_display_name(display_name))
    _add(short)
    _add(normalize_project_text(display_name))
    for normalized_name in (short, normalize_project_text(display_name)):
        stem = normalized_name[4:] if normalized_name.startswith("the ") else normalized_name
        token = _WORD_RUN.search(stem)
        if token:
            # 'the soleil da nang' -> 'the soleil' (prefix) and 'soleil' (token)
            _add(normalized_name[: token.end()])
            _add(token.group(0))
    return tuple(variants)


def build_redirect(project_key: str, display_name: str) -> dict[str, str]:
    """Structured switch signal the FE renders a project-switch button from.

    ``display_name`` is the picker label (marketing qualifier already
    stripped); ``short_name`` additionally drops the trailing city token so
    compact UI copy reads 'The Soleil' instead of 'The Soleil Đà Nẵng'.
    """
    label = short_display_name(display_name)
    return {
        "project_key": project_key,
        "display_name": label,
        "short_name": strip_city_suffix(label),
    }


def redirect_message(display_name: str) -> str:
    """Guidance answer text for a redirect turn (no corpus claims inside)."""
    return REDIRECT_MESSAGE_TEMPLATE.format(
        display_name=short_display_name(display_name),
        short_name=strip_city_suffix(short_display_name(display_name)),
    )


def detect_foreign_project(
    query: str,
    current_project_key: str | None,
    known_projects: Iterable[Sequence[str]],
) -> dict[str, str] | None:
    """First known NON-current project named in the query, else None.

    ``known_projects`` is an ordered sequence of ``(project_key, display_name)``
    pairs (registry catalogue order). Rules pinned by the guardrail contract:

    - Only the current project mentioned (or nothing recognizable) -> None, the
      normal flow proceeds.
    - Any OTHER known project mentioned -> redirect to the first such project
      in catalogue order.
    - Comparison questions naming BOTH projects still redirect: a single
      project's corpus can never ground a cross-project comparison.
    - Unknown third-party names ('vinhomes') never match — they are absent
      from the known list — so existing behavior is preserved.
    - An unbound session project (current None) treats ANY known-project
      mention as foreign: with no scoped corpus there is nothing to answer
      from, so guiding the pick is strictly safer than guessing.
    """
    normalized_query = normalize_project_text(query)
    if not normalized_query:
        return None
    current = current_project_key or ""
    for project_key, display_name in known_projects:
        if project_key == current:
            continue
        variants = project_match_variants(project_key, display_name)
        if not any(v in normalized_query for v in variants):
            continue
        return build_redirect(project_key, display_name)
    return None


def known_projects_payload(pairs: Iterable[Sequence[str]]) -> list[dict[str, Any]]:
    """Map (key, display_name) pairs onto the redirect-signal dict shape."""
    return [build_redirect(key, name) for key, name in pairs]


__all__ = [
    "REDIRECT_MESSAGE_TEMPLATE",
    "normalize_project_text",
    "short_display_name",
    "strip_city_suffix",
    "project_match_variants",
    "build_redirect",
    "redirect_message",
    "detect_foreign_project",
    "known_projects_payload",
]
