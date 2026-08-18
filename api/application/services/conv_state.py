"""Conversation state: ConvContext LRU + state machine + CTA policy (Story 4.5, §6.2/6.5).

In-memory LRU 512 sessions / TTL 2h (open item #17 — MVP). System consequence:
`uvicorn --workers 1` only — LRU + in-memory rate limits do not survive multi-
worker (runbook + docker compose note, plan §6.2). The post-MVP
`conversation_state` table mirrors this dataclass; Epic 4 never touches it.

States: greet | qualify | recommend | nurture | handoff_done.
Transition table (plan §6.2) — legal/other side-track never changes state.
CTA policy (§6.5): lead_cta_hint emits when (a) useful_turns≥1, (b) no
phone_given, (c) last_cta_turn ≥2 away, (d) answer not requires_review,
(e) ≤3 per session. Hint rotates through CTA_VARIANTS (touchpoint ~5.7:
"chuyên viên gọi trong ~5 phút").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from api.domain.services.route_intent import Intent

SESSIONS_MAX = 512
TTL_SECONDS = 2 * 3600  # 2h

# Constant-time eviction: insertion-ordered dict + move-to-end on touch.
class _LRU(dict):
    def touch(self, key: str) -> None:
        if key in self:
            self[key] = self.pop(key)

    def put(self, key: str, value: Any) -> None:
        self[key] = value
        if len(self) > SESSIONS_MAX:
            self.pop(next(iter(self)), None)


_LRU_CACHE: _LRU = _LRU()
_ANON_SEQ = [0]  # monotonic counter for anonymous session keys


@dataclass
class ConvContext:
    session_id: str
    state: str = "greet"
    slots: dict = field(default_factory=dict)   # budget_vnd, bedrooms, view, timeline, purpose, phone_given
    useful_turns: int = 0                        # turns with facts/card/answer value
    last_cta_turn: "int | None" = None
    cta_shown_count: int = 0
    interested_units: list = field(default_factory=list)
    turn: int = 0
    updated_at: float = field(default_factory=time.time)


CTA_VARIANTS = (
    "Anh/chị để lại số điện thoại nhé, chuyên viên gọi lại trong ~5 phút để tư vấn căn phù hợp.",
    "Để chuyên viên gọi tư vấn nhanh 5 phút, anh/chị cho em xin số điện thoại với ạ?",
    "Em nhờ chuyên viên gọi lại trong ~5 phút cho mình — anh/chị gửi số điện thoại giúp em nhé.",
)

# Slot vocabulary — keep single-source with rewrite/route_intent where they
# already exist; add conv-specific (view/timeline/purpose) keywords here.
_VIEW_KEYWORDS = ("view biển", "view hồ", "view núi", "biển", "hồ bơi", "công viên", "nội khu")
_TIMELINE_KEYWORDS = ("gấp", "tháng này", "cuối năm", "sau tết", "trong năm nay", "năm sau")
_PURPOSE_STAY = ("ở thật", "an cư", "để ở", "tự ở", "làm nhà", "gia đình ở")
_PURPOSE_INVEST = ("đầu tư", "cho thuê", "kinh doanh", "lướt sóng", "mua đi bán lại")

# --- LRU access -------------------------------------------------------------

def get_context(session_id: "str | None") -> ConvContext:
    """Return the live context, creating a fresh one when absent/expired.

    Guards against None/empty session_id (edge-case): a missing id must never
    collapse into a shared cache key — each anonymous caller gets its own
    context, so no cross-session state bleed.
    """
    if not session_id:
        _ANON_SEQ[0] += 1
        session_id = f"anon-{_ANON_SEQ[0]}-{time.time_ns()}"
    now = time.time()
    _LRU_CACHE.touch(session_id)
    ctx = _LRU_CACHE.get(session_id)
    if ctx is None or (now - ctx.updated_at) > TTL_SECONDS:
        ctx = ConvContext(session_id=session_id)
        _LRU_CACHE.put(session_id, ctx)
    return ctx


def mark_phone_given(session_id: str) -> None:
    """POST /api/lead ok -> phone_given + handoff_done (plan §6.7, 1 line in lead svc)."""
    ctx = get_context(session_id)
    ctx.slots["phone_given"] = True
    ctx.state = "handoff_done"
    ctx.updated_at = time.time()


def _evict_expired() -> int:
    now = time.time()
    dead = [k for k, v in _LRU_CACHE.items() if (now - v.updated_at) > TTL_SECONDS]
    for k in dead:
        _LRU_CACHE.pop(k, None)
    return len(dead)


# --- State machine (plan §6.2 table) ---------------------------------------

def transition(ctx: ConvContext, intent: Intent, new_slot: bool = False,
               afford_answered: bool = False) -> None:
    """Advance state per the plan §6.2 table.

    Side-tracks (legal/other/location/closure) keep state — the funnel is never
    pushed by legal-only or politeness turns (FIX-7). Events, in priority order:
      1. handoff_done row: confirm-waiting, never leaves;
      2. afford answered   -> recommend (from every non-terminal state);
      3. new slot / mua intent -> recommend from (recommend, nurture), else qualify;
      4. >=3 useful turns without phone -> nurture from (recommend, nurture),
         qualify otherwise (greet/qualify rows).
    """
    old = ctx.state
    if intent in (Intent.LEGAL, Intent.OTHER, Intent.LOCATION, Intent.CLOSURE):
        ctx.turn += 1
        ctx.updated_at = time.time()
        return  # side-track / FIX-7: funnel unchanged
    if old == "handoff_done":
        ctx.state = "handoff_done"  # xác nhận chờ gọi — không đổi
    elif afford_answered:
        ctx.state = "recommend"
    elif new_slot:
        ctx.state = "recommend" if old in ("recommend", "nurture") else "qualify"
    elif ctx.useful_turns >= 3 and not ctx.slots.get("phone_given"):
        ctx.state = "nurture" if old in ("recommend", "nurture") else "qualify"
    ctx.turn += 1
    ctx.updated_at = time.time()
    return


# --- CTA policy (§6.5) ------------------------------------------------------

def maybe_lead_cta_hint(ctx: ConvContext, requires_review: bool = False) -> "str | None":
    """All gates (a)-(e) must hold, else None. Rotates the hint variant."""
    if ctx.useful_turns < 1:          # (a)
        return None
    if ctx.slots.get("phone_given"):  # (b)
        return None
    if ctx.last_cta_turn is not None and ctx.turn - ctx.last_cta_turn < 2:  # (c)
        return None
    if requires_review:               # (d)
        return None
    if ctx.cta_shown_count >= 3:      # (e)
        return None
    variant = CTA_VARIANTS[ctx.cta_shown_count % len(CTA_VARIANTS)]
    ctx.cta_shown_count += 1
    ctx.last_cta_turn = ctx.turn
    return variant


def note_useful_turn(ctx: ConvContext) -> None:
    """Increment useful_turns for a turn that produced facts/card/answer value."""
    ctx.useful_turns += 1


def conv_directive(state: str) -> str:
    """§6.4 per-state CONVERSATION_DIRECTIVE (system-role dynamic message)."""
    return {
        "greet": "Lượt này: chào ấm 1 câu + trả lời + MỘT câu hỏi mở về nhu cầu (loại căn / để ở hay đầu tư).",
        "qualify": "Trả lời + hỏi ĐÚNG MỘT slot còn thiếu, ưu tiên budget → bedrooms → timeline → purpose.",
        "recommend": "Trả lời + so sánh tối đa 3 căn từ evidence + MỘT dòng mời nhận tư vấn căn phù hợp.",
        "nurture": "Recap 2 giá trị khách quan tâm + MỘT lời mời nhận cuộc gọi 5 phút (CTA bản rõ, 1 lần).",
        "handoff_done": "Xác nhận chuyên viên sẽ gọi trong ~5 phút, không hỏi thêm; hỗ trợ thêm nếu khách hỏi.",
    }.get(state, "Trả lời chính xác bằng evidence; không bịa số.")


def register_interest(ctx: ConvContext, unit_key: "str | None") -> None:
    if unit_key and unit_key not in ctx.interested_units:
        ctx.interested_units.append(unit_key)


__all__ = [
    "ConvContext", "CTA_VARIANTS", "SESSIONS_MAX", "TTL_SECONDS",
    "get_context", "mark_phone_given", "transition", "maybe_lead_cta_hint",
    "note_useful_turn", "conv_directive", "register_interest",
]
