"""Deterministic pre-rewrite intent classifier (ADR-0002 D1 + D3, Story 4.1).

Runs right after the L1 guard, before rewrite. Short-circuits HANDOFF /
COMPANY / LOCATION (no rewrite/RAG/SQL/generate), lets PRICE / LEGAL / OTHER
fall through to the LLM router. Pure and synchronous — fully unit-testable,
and it reuses the keyword sets defined in ``api.rewrite`` instead of duplicating
them (SOLID: one source of routing vocabulary).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .rewrite import AGGREGATE_KEYWORDS, GEO_INTENT_KEYWORDS, HIGH_STAKES_KEYWORDS

# Hard-short-cut intents: their answer needs no RAG/SQL/rewrite (Story 4.1).
# CLOSURE is the politeness terminator (FIX-7): never drop a real follow-up.
class Intent(str, Enum):
    HANDOFF = "handoff"  # needs a human sales call
    COMPANY = "company"  # company/developer facts
    LOCATION = "location"  # geospatial — maps leg
    PRICE = "price"  # affordability — falls through to rewrite/sql
    LEGAL = "legal"  # legal lookup — falls through to rewrite/rag
    OTHER = "other"
    CLOSURE = "closure"  # thanks/ok with no real follow-up


@dataclass(frozen=True)
class ClassifyResult:
    intent: Intent
    reason: str  # 'matched:keyword' or 'default'


# Human-contact / phone-lead signals (Epic 6 target) — strongest short-circuit.
# "tư vấn" ALONE is too loose: "chi phí tư vấn pháp lý" is a price/legal question, not
# a contact request. Require a contact-desire form (regex-vs-llm safety: narrow the
# deterministic trigger so a false positive never sends a real question to the lead
# form). Strong signals still always match.
_HANDOFF_KEYWORDS = (
    "gọi điện", "gọi lại", "liên hệ", "gặp sales", "gặp tư vấn", "gặp trực tiếp",
    "nhân viên kinh doanh", "số điện thoại", "sđt", "hotline", "để lại số",
    "nhờ tư vấn", "cần tư vấn", "muốn tư vấn", "nhận tư vấn", "tư vấn trực tiếp",
    "chuyên viên tư vấn", "từ chuyên viên",
    "tôi muốn đặt cọc", "đặt cọc", "mua liền", "ký hợp đồng",
    "hẹn xem", "xem thực tế", "xem nhà", "báo giá", "gửi bảng giá",
)

# Company/developer info (no geo, no price). The "X là của ai" ownership phrase
# is project-specific (story 10.2): the brand token is appended per request so
# a Soleil user asking about Soleil still routes to COMPANY.
_COMPANY_KEYWORDS = (
    "chủ đầu tư", "công ty", "địa ốc", "thành lâm", "developer", "doanh nghiệp",
    "đơn vị phát triển", "chủ dự án", "ai là chủ",
)


def _company_keywords(project_name: "str | None" = None) -> tuple[str, ...]:
    """Company keywords plus the active project's ownership phrase.

    ``project_name`` is the brand token (e.g. 'camellia' / 'soleil'); None keeps
    the legacy Camellia token so pre-project callers classify identically.
    """
    token = project_name or "camellia"
    return _COMPANY_KEYWORDS + (f"{token} là của ai",)

# Price-relevant vocabulary — complements (not replaces) the LLM router.
_PRICE_KEYWORDS = (
    "giá", "bao nhiêu tiền", "chi phí", "trả góp", "vay", "tiền cọc", "cọc",
    "thanh toán", "chiết khấu", "đợt giá", "bảng giá", "giá bán", "triệu", "tỷ",
    "phương thức", "htls", "vốn", "budget", "ngân sách", "afford",
)

# Politeness/closure vocabulary (FIX-7).
_CLOSURE_WORDS = ("cảm ơn", "ok", "okey", "okê", "ừ", "uh", "được rồi", "thế thôi", "hết")
_FOLLOWUP_MARKERS = ("?", "hỏi", "cho hỏi", "bao nhiêu", "giá", "nào", "là gì", "còn không", "thì sao", "sao?")
# Words/phrases meaning the user is NOT done — they want to act/continue/deliberate.
# "ok để em xem thực tế" / "ok về bàn với chồng" / "cảm ơn để tôi suy nghĩ" are not
# closure: they carry an onward action. FIX-7 must never drop these as closing.
_CLOSURE_BLOCKERS = (
    "xem thực tế", "xem nhà", "hẹn", "bàn", "bàn với", "suy nghĩ", "đi xem",
    "gọi", "liên hệ", "đặt cọc", "ký", "tư vấn", "hỏi", "giá", "còn", "thì",
    "cho em", "giúp em",
)
# Location questions phrased as a suffix ("ở đâu", "chỗ nào") that the static
# GEO_INTENT_KEYWORDS list does not cover.
_LOCATION_SUFFIXES = ("ở đâu", "chỗ nào", "vị trí", "khu nào")


def _contains(text: str, keywords: tuple[str, ...]) -> bool:
    q = (text or "").lower()
    return any(k in q for k in keywords)


def classify_intent(
    query: str, history: list[dict] | None = None, project_name: "str | None" = None
) -> ClassifyResult:
    """Classify a raw query into one Intent; CLOSURE when it is pure politeness.

    ``project_name`` (story 10.2) is the active project's brand token used to
    build the company-ownership keyword; None keeps the legacy Camellia token.

    Precedence: closure check (politeness must not hide a real question, so a
    follow-up marker overrides CLOSURE) -> HANDOFF -> COMPANY -> LOCATION ->
    PRICE/LEGAL -> OTHER. The LLM router still decides price-vs-legal nuance.
    """
    q = (query or "").strip()
    is_closure_like = any(
        q.lower().startswith(w) or q.lower() == w for w in _CLOSURE_WORDS
    )
    if is_closure_like:
        # FIX-7: politeness must not drop a real question — a follow-up marker
        # anywhere in the same turn keeps the conversation going.
        combined = (q + " " + " ".join(
            str(t.get("content", "")) for t in (history or []) if t.get("role") == "user"
        )).lower()
        if (
            not _contains(combined, _FOLLOWUP_MARKERS)
            and not _contains(q, _CLOSURE_BLOCKERS)
            and _contains(q, _CLOSURE_WORDS)
        ):
            return ClassifyResult(Intent.CLOSURE, "closure:no_followup")

    if _contains(q, _HANDOFF_KEYWORDS):
        return ClassifyResult(Intent.HANDOFF, "matched:handoff")
    if _contains(q, _company_keywords(project_name)):
        return ClassifyResult(Intent.COMPANY, "matched:company")
    if _contains(q, GEO_INTENT_KEYWORDS) or _contains(q, _LOCATION_SUFFIXES):
        return ClassifyResult(Intent.LOCATION, "matched:geo")
    if _contains(q, _PRICE_KEYWORDS):
        return ClassifyResult(Intent.PRICE, "matched:price")
    if _contains(q, HIGH_STAKES_KEYWORDS):
        return ClassifyResult(Intent.LEGAL, "matched:legal")
    return ClassifyResult(Intent.OTHER, "default")