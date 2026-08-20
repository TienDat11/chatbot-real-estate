"""Slot extraction: budget/bedrooms/view/timeline/purpose (Story 4.5, 6.3).

Deterministic first, then one LLM JSON call for the leftover slots when the
query has >= 6 words and a slot is still empty. Fail-open: on any LLM error
the empty slots stay empty and the pipeline proceeds — a failed slot-fill
never blocks a conversation turn.

The extracted slots feed (a) the per-state CONVERSATION_DIRECTIVE, and
(b) POST /api/lead prefill ("Ngân sách: 4 tỷ · Quan tâm: 2PN").
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from api.domain.entities.price_calc import extract_budget
from api.infrastructure.dependencies import get_llm, model_for_role


def _clean_json(text: str) -> str:
    """Strip a ```json fence and extract the first JSON object in the text."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        return t[start : end + 1]
    return t

logger = logging.getLogger("api.conv_slots")

# Plan §6.3 keyword sets (conv-specific; budget reuses price_calc).
_BEDROOM_RE = re.compile(r"(\bstudio\b|\b[123]\s?pn\b|\b[123]\s?phòng ngủ\b)", re.IGNORECASE)
_VIEW_KEYWORDS = ("view biển", "view hồ bơi", "view hồ", "view núi", "view sông", "hồ bơi", "biển", "công viên", "nội khu")
_TIMELINE_KEYWORDS = ("gấp", "tháng này", "cuối năm", "sau tết", "trong năm nay", "năm sau", "sang năm")
# Purpose labels (plan §6.3, expanded to 5 groups per sales follow-up):
# stay / invest / rent / office / hotel. Order matters: a keyword can only map
# to one label, so run more-specific groups (rent/office/hotel) before stay/invest.
_PURPOSE_STAY = ("ở thật", "an cư", "để ở", "tự ở", "làm nhà", "gia đình ở", "để gia đình", "mua để ở")
_PURPOSE_INVEST = ("đầu tư", "kinh doanh", "lướt sóng", "mua đi bán lại", "sinh lời", "tăng giá", "lời", "tích sản")
_PURPOSE_RENT = ("cho thuê", "mua cho thuê", "để cho thuê", "cho thuê lại", "khai thác cho thuê", "làm homestay", "airbnb")
_PURPOSE_OFFICE = ("làm văn phòng", "văn phòng", "công ty", "mở công ty", "trụ sở", "làm văn phòng công ty")
_PURPOSE_HOTEL = ("làm khách sạn", "khách sạn", "mở khách sạn", "kinh doanh lưu trú", "căn hộ khách sạn", "condotel")


def extract_bedrooms(query: str) -> "str | None":
    """Normalize bedroom reference: 2pn/2 PN/2 phòng ngủ -> 2PN."""
    m = _BEDROOM_RE.search(query or "")
    if not m:
        return None
    raw = m.group(1).lower().replace("phòng ngủ", "pn")
    raw = raw.replace(" ", "").upper()
    return raw if raw in ("STUDIO", "1PN", "2PN", "3PN") else None


def extract_view(query: str) -> "str | None":
    q = (query or "").lower()
    for kw in _VIEW_KEYWORDS:
        if kw in q:
            return kw
    return None


def extract_timeline(query: str) -> "str | None":
    q = (query or "").lower()
    for kw in _TIMELINE_KEYWORDS:
        if kw in q:
            return kw
    return None


def extract_purpose(query: str) -> "str | None":
    q = (query or "").lower()
    # Negation-aware: "không đầu tư" must never produce invest.
    if re.search(r"không\s+(đầu tư|mua đầu tư)", q):
        q_clean = re.sub(r"không\s+(đầu tư|mua đầu tư)\s*", " ", q)
    else:
        q_clean = q
    # Specific sub-groups first so "cho thuê" maps to rent (not invest),
    # "làm khách sạn" to hotel, and "làm văn phòng" to office before stay.
    for kw in _PURPOSE_RENT:
        if kw in q_clean:
            return "rent"
    for kw in _PURPOSE_OFFICE:
        if kw in q_clean:
            return "office"
    for kw in _PURPOSE_HOTEL:
        if kw in q_clean:
            return "hotel"
    for kw in _PURPOSE_STAY:
        if kw in q_clean:
            return "stay"
    for kw in _PURPOSE_INVEST:
        if kw in q_clean:
            return "invest"
    return None


# Deterministic slots: (field, extractor) — cheap and pure.
_FIELD_EXTRACTORS: "dict[str, Any]" = {
    "budget_vnd": extract_budget,
    "bedrooms": extract_bedrooms,
    "view": extract_view,
    "timeline": extract_timeline,
    "purpose": extract_purpose,
}

_SLOT_FIELDS: "tuple[str, ...]" = tuple(_FIELD_EXTRACTORS)


def extract_slots_deterministic(query: str) -> "dict[str, Any]":
    """All deterministic slot fields from one query string."""
    out: dict[str, Any] = {}
    for field, fn in _FIELD_EXTRACTORS.items():
        try:
            v = fn(query)
        except Exception:  # noqa: BLE001 — slot extraction never throws
            v = None
        if v is not None:
            out[field] = v
    return out


_EXTRACT_SYSTEM = (
    "Bạn là bộ tách dữ liệu khách quan. Từ tin nhắn người dùng, trả về JSON với các field "
    "budget_vnd (số VND hoặc null), bedrooms (null|studio|1PN|2PN|3PN), view (null|chuỗi ngắn), "
    "timeline (null|gấp|tháng này|cuối năm|sau tết|năm sau), purpose (null|stay|invest|rent|office|hotel). "
    "Chỉ điền field chắc chắn; KHÔNG bịa; không thêm field khác."
)


async def llm_slot_fill(query: str) -> "dict[str, Any]":
    """One JSON-mode LLM call for leftover slots; fail-open (returns {} on error)."""
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": query},
    ]
    model = model_for_role("extract")
    try:
        llm = get_llm()
        text = await llm.complete(messages, json_mode=True, model=model, timeout=6.0)
        data = json.loads(_clean_json(text))
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in _SLOT_FIELDS and v not in (None, "")}
    except Exception as exc:  # noqa: BLE001 — fail-open, never block the pipeline
        logger.warning("llm slot-fill failed (fail-open): %s", exc)
        return {}


async def extract_slots(query: str, current: "dict[str, Any] | None" = None) -> "dict[str, dict[str, Any]]":
    """Merge deterministic + LLM slots. Returns {deterministic, llm, merged}.

    - Deterministic always runs (cheap, pure).
    - LLM runs only when the query has >= 6 words AND some slot is still empty.
    - Never overrides an existing phone_given (plan §6.3).
    """
    current = dict(current or {})
    det = extract_slots_deterministic(query)
    merged = {**current, **det}
    words = len((query or "").split())
    llm_out: dict[str, Any] = {}
    if words >= 6 and any(f not in merged for f in _SLOT_FIELDS):
        llm_out = await llm_slot_fill(query)
        merged = {**merged, **llm_out}
    if "phone_given" in current:  # never overwritten by slot extraction
        merged["phone_given"] = current["phone_given"]
    return {"deterministic": det, "llm": llm_out, "merged": merged}


def lead_prefill_note(slots: "dict[str, Any]") -> str:
    """§6.3 note for POST /api/lead prefill, e.g. Ngân sách: 4 tỷ · Quan tâm: 2PN."""
    parts: list[str] = []
    budget = slots.get("budget_vnd")
    if isinstance(budget, (int, float)) and budget > 0:
        if budget >= 1e9:
            val = f"{budget/1e9:.1f}".rstrip("0").rstrip(".")
            parts.append(f"Ngân sách: {val} tỷ")
        else:
            val = f"{budget/1e6:.0f}"
            parts.append(f"Ngân sách: {val} triệu")
    if slots.get("bedrooms"):
        parts.append(f"Quan tâm: {slots['bedrooms']}")
    if slots.get("view"):
        parts.append(f"View: {slots['view']}")
    if slots.get("timeline"):
        parts.append(f"Tiến độ: {slots['timeline']}")
    if slots.get("purpose") == "invest":
        parts.append("Mục đích: đầu tư")
    elif slots.get("purpose") == "stay":
        parts.append("Mục đích: để ở")
    elif slots.get("purpose") == "rent":
        parts.append("Mục đích: cho thuê")
    elif slots.get("purpose") == "office":
        parts.append("Mục đích: làm văn phòng")
    elif slots.get("purpose") == "hotel":
        parts.append("Mục đích: làm khách sạn")
    return " · ".join(parts)


__all__ = [
    "extract_bedrooms", "extract_view", "extract_timeline", "extract_purpose",
    "extract_slots_deterministic", "llm_slot_fill", "extract_slots",
    "lead_prefill_note",
]
