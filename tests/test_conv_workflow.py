"""Story 4.5 — Conversation engine: ConvContext state machine + qualification + CTA policy.

Verifies:
1. State machine transitions (12 cases per §6.2 transition table).
2. Slot extraction (15 cases deterministic + fallbacks).
3. CTA policy gating (8 cases for conditions a-e + variant rotation).
4. FIX-7 regression: CLOSURE / side-track never changes funnel state.
5. SSE routing event payload contract (intent, conv_state, panel_hint, lead_cta_hint).
6. Facade back-compat (RagQueryPipeline / RagQueryPipelineConv).
7. LRU capacity bounding + eviction (SESSIONS_MAX=512).
8. CONVERSATION_DIRECTIVE text for all 5 states.
"""

import time
import pytest
from unittest.mock import AsyncMock, patch

from api.application.services.conv_state import (
    ConvContext,
    CTA_VARIANTS,
    SESSIONS_MAX,
    conv_directive,
    get_context,
    mark_phone_given,
    maybe_lead_cta_hint,
    note_useful_turn,
    transition,
    _LRU_CACHE,
)
from api.domain.services.conv_slots import (
    extract_bedrooms,
    extract_purpose,
    extract_slots_deterministic,
    extract_timeline,
    extract_view,
    lead_prefill_note,
)
from api.domain.services.route_intent import Intent
from api.application.pipelines.conv_workflow import (
    RagRgreConvWorkflow,
    RagQueryPipelineConv,
    SSE_EVENT_ROUTING,
    _panel_hint,
)
from api.application.services.generate import build_messages
from api.application.services.merge import Merged


# ==============================================================================
# 1. State Machine Transitions (12 cases per plan §6.2)
# ==============================================================================

def test_transition_greet_to_qualify_on_new_slot():
    ctx = ConvContext(session_id="s1", state="greet")
    transition(ctx, Intent.PRICE, new_slot=True, afford_answered=False)
    assert ctx.state == "qualify"
    assert ctx.turn == 1


def test_transition_greet_to_recommend_on_afford_answered():
    ctx = ConvContext(session_id="s2", state="greet")
    transition(ctx, Intent.PRICE, new_slot=True, afford_answered=True)
    assert ctx.state == "recommend"


def test_transition_greet_3_useful_turns_no_phone():
    ctx = ConvContext(session_id="s3", state="greet", useful_turns=3)
    transition(ctx, Intent.PRICE, new_slot=False, afford_answered=False)
    assert ctx.state == "qualify"


def test_transition_qualify_to_recommend_on_afford_answered():
    ctx = ConvContext(session_id="s4", state="qualify")
    transition(ctx, Intent.PRICE, new_slot=False, afford_answered=True)
    assert ctx.state == "recommend"


def test_transition_qualify_to_qualify_on_new_slot():
    ctx = ConvContext(session_id="s5", state="qualify")
    transition(ctx, Intent.PRICE, new_slot=True, afford_answered=False)
    assert ctx.state == "qualify"


def test_transition_qualify_3_useful_turns():
    ctx = ConvContext(session_id="s6", state="qualify", useful_turns=3)
    transition(ctx, Intent.PRICE, new_slot=False, afford_answered=False)
    assert ctx.state == "qualify"


def test_transition_recommend_to_recommend_on_new_slot():
    ctx = ConvContext(session_id="s7", state="recommend")
    transition(ctx, Intent.PRICE, new_slot=True, afford_answered=False)
    assert ctx.state == "recommend"


def test_transition_recommend_to_nurture_on_3_useful_turns():
    ctx = ConvContext(session_id="s8", state="recommend", useful_turns=3)
    transition(ctx, Intent.PRICE, new_slot=False, afford_answered=False)
    assert ctx.state == "nurture"


def test_transition_nurture_to_recommend_on_new_slot():
    ctx = ConvContext(session_id="s9", state="nurture")
    transition(ctx, Intent.PRICE, new_slot=True, afford_answered=False)
    assert ctx.state == "recommend"


def test_transition_nurture_stays_nurture_on_3_useful_turns():
    ctx = ConvContext(session_id="s10", state="nurture", useful_turns=3)
    transition(ctx, Intent.PRICE, new_slot=False, afford_answered=False)
    assert ctx.state == "nurture"


def test_transition_handoff_done_stays_terminal():
    ctx = ConvContext(session_id="s11", state="handoff_done")
    transition(ctx, Intent.PRICE, new_slot=True, afford_answered=True)
    assert ctx.state == "handoff_done"


def test_mark_phone_given_sets_state_and_flag():
    ctx = get_context("s_phone")
    mark_phone_given("s_phone")
    assert ctx.slots.get("phone_given") is True
    assert ctx.state == "handoff_done"


# ==============================================================================
# 2. Side-tracks & FIX-7 Regressions
# ==============================================================================

def test_sidetrack_legal_preserves_state():
    ctx = ConvContext(session_id="s_leg", state="qualify")
    transition(ctx, Intent.LEGAL, new_slot=True, afford_answered=True)
    assert ctx.state == "qualify"
    assert ctx.turn == 1


def test_sidetrack_closure_fix_7():
    ctx = ConvContext(session_id="s_clo", state="recommend")
    transition(ctx, Intent.CLOSURE, new_slot=True, afford_answered=True)
    assert ctx.state == "recommend"
    assert ctx.turn == 1


def test_sidetrack_location_and_other():
    ctx = ConvContext(session_id="s_loc", state="nurture")
    transition(ctx, Intent.LOCATION, new_slot=True)
    assert ctx.state == "nurture"
    transition(ctx, Intent.OTHER, new_slot=True)
    assert ctx.state == "nurture"
    assert ctx.turn == 2


# ==============================================================================
# 3. Slot Extraction (15 cases)
# ==============================================================================

@pytest.mark.parametrize(
    ("query", "expected_bed"),
    [
        ("cho hỏi căn 2PN", "2PN"),
        ("căn 2 phòng ngủ giá sao", "2PN"),
        ("tôi muốn mua căn 1pn", "1PN"),
        ("căn 1 phòng ngủ", "1PN"),
        ("xem căn 3PN", "3PN"),
        ("căn studio có không", "STUDIO"),
        ("không đề cập phòng", None),
    ],
)
def test_extract_bedrooms(query, expected_bed):
    assert extract_bedrooms(query) == expected_bed


@pytest.mark.parametrize(
    ("query", "expected_view"),
    [
        ("căn view biển đẹp", "view biển"),
        ("có căn view hồ bơi không", "view hồ bơi"),
        ("căn view công viên", "công viên"),
        ("view núi sơn trà", "view núi"),
        ("không có view", None),
    ],
)
def test_extract_view(query, expected_view):
    assert extract_view(query) == expected_view


@pytest.mark.parametrize(
    ("query", "expected_timeline"),
    [
        ("cần mua gấp", "gấp"),
        ("dự kiến tháng này", "tháng này"),
        ("tầm cuối năm nay", "cuối năm"),
        ("sau tết em mua", "sau tết"),
        ("năm sau", "năm sau"),
        ("không vội", None),
    ],
)
def test_extract_timeline(query, expected_timeline):
    assert extract_timeline(query) == expected_timeline


@pytest.mark.parametrize(
    ("query", "expected_purpose"),
    [
        ("mua để ở thật", "stay"),
        ("an cư lâu dài", "stay"),
        ("mua để đầu tư cho thuê", "invest"),
        ("mua kinh doanh", "invest"),
        ("chỉ tham khảo", None),
    ],
)
def test_extract_purpose(query, expected_purpose):
    assert extract_purpose(query) == expected_purpose


def test_extract_slots_deterministic_full():
    q = "Tôi có 3.5 tỷ cần mua gấp căn 2PN view biển để ở"
    slots = extract_slots_deterministic(q)
    assert slots.get("budget_vnd") == 3_500_000_000
    assert slots.get("bedrooms") == "2PN"
    assert slots.get("view") == "view biển"
    assert slots.get("timeline") == "gấp"
    assert slots.get("purpose") == "stay"
    note = lead_prefill_note(slots)
    assert "Ngân sách: 4 tỷ" in note or "Ngân sách: 3.5" in note or "3 tỷ" in note
    assert "Quan tâm: 2PN" in note
    assert "View: view biển" in note
    assert "Tiến độ: gấp" in note
    assert "Mục đích: để ở" in note


# ==============================================================================
# 4. CTA Policy Gating (8 cases for conditions a-e)
# ==============================================================================

def test_cta_gate_a_requires_useful_turns():
    ctx = ConvContext(session_id="cta1", useful_turns=0, turn=1)
    assert maybe_lead_cta_hint(ctx) is None
    ctx.useful_turns = 1
    assert maybe_lead_cta_hint(ctx) is not None


def test_cta_gate_b_blocked_if_phone_given():
    ctx = ConvContext(session_id="cta2", useful_turns=1, turn=1, slots={"phone_given": True})
    assert maybe_lead_cta_hint(ctx) is None


def test_cta_gate_c_spacing_at_least_2_turns():
    ctx = ConvContext(session_id="cta3", useful_turns=1, turn=2, last_cta_turn=1)
    assert maybe_lead_cta_hint(ctx) is None  # turn 2 - 1 = 1 (< 2)
    ctx.turn = 3  # turn 3 - 1 = 2 (>= 2)
    assert maybe_lead_cta_hint(ctx) is not None


def test_cta_gate_d_requires_review_blocks():
    ctx = ConvContext(session_id="cta4", useful_turns=1, turn=2)
    assert maybe_lead_cta_hint(ctx, requires_review=True) is None
    assert maybe_lead_cta_hint(ctx, requires_review=False) is not None


def test_cta_gate_e_max_3_times_per_session():
    ctx = ConvContext(session_id="cta5", useful_turns=1, turn=1, cta_shown_count=0)
    hint1 = maybe_lead_cta_hint(ctx)
    assert hint1 in CTA_VARIANTS
    ctx.turn = 4
    hint2 = maybe_lead_cta_hint(ctx)
    assert hint2 in CTA_VARIANTS
    ctx.turn = 7
    hint3 = maybe_lead_cta_hint(ctx)
    assert hint3 in CTA_VARIANTS
    ctx.turn = 10
    assert maybe_lead_cta_hint(ctx) is None  # 4th attempt rejected


def test_cta_variant_rotation():
    ctx = ConvContext(session_id="cta_rot", useful_turns=1, turn=1, cta_shown_count=0)
    h1 = maybe_lead_cta_hint(ctx)
    ctx.turn = 3
    h2 = maybe_lead_cta_hint(ctx)
    assert h1 != h2
    assert h1 == CTA_VARIANTS[0]
    assert h2 == CTA_VARIANTS[1]


# ==============================================================================
# 5. Panel Hints & SSE Contract
# ==============================================================================

def test_panel_hints():
    assert _panel_hint(Intent.LOCATION) == "map"
    assert _panel_hint(Intent.COMPANY) == "company"
    assert _panel_hint(Intent.PRICE) == "affordability"
    assert _panel_hint(Intent.HANDOFF) == "lead"
    assert _panel_hint(Intent.LEGAL) == "none"
    assert _panel_hint(Intent.OTHER) == "none"


# ==============================================================================
# 6. LRU Cache Bounding & Eviction
# ==============================================================================

def test_lru_cache_bounds_to_max():
    # Insert 600 unique sessions; verify the LRU caps at SESSIONS_MAX (not just <=).
    for i in range(600):
        get_context(f"sess_lru_{i}")
    assert len(_LRU_CACHE) == SESSIONS_MAX
    # old keys evicted, newest kept
    assert f"sess_lru_{SESSIONS_MAX - 1}" in _LRU_CACHE
    assert "sess_lru_0" not in _LRU_CACHE


# ==============================================================================
# 7. CONVERSATION_DIRECTIVE text tests
# ==============================================================================

def test_conversation_directives_defined():
    for state in ("greet", "qualify", "recommend", "nurture", "handoff_done"):
        d = conv_directive(state)
        assert d, f"directive missing for {state}"
        assert isinstance(d, str)
        if state == "greet":
            assert "chào ấm 1 câu" in d
        elif state == "qualify":
            assert "ưu tiên budget" in d
        elif state == "recommend":
            assert "so sánh tối đa 3 căn" in d
        elif state == "nurture":
            assert "Recap 2 giá trị" in d
        elif state == "handoff_done":
            assert "~5 phút" in d


# ==============================================================================
# 8. CONVERSATION_DIRECTIVE Injection in build_messages
# ==============================================================================

def test_build_messages_includes_directive_when_present():
    merged = Merged(
        rag_blocks="RAG",
        evidence_blocks="EVIDENCE",
        sources=[],
        facts=[],
        meta={
            "query": "tôi có 3 tỷ",
            "rewritten": "tôi có 3 tỷ",
            "conversation_directive": "Trả lời + hỏi ĐÚNG MỘT slot còn thiếu",
        },
    )
    msgs = build_messages(merged, None)
    system_msgs = [m["content"] for m in msgs if m["role"] == "system"]
    assert any("CONVERSATION_DIRECTIVE" in s for s in system_msgs)
    assert any("hỏi ĐÚNG MỘT slot còn thiếu" in s for s in system_msgs)


# ==============================================================================
# 9. INTEGRATION: conv workflow facade + SSE routing (closes verification gap)
#    These exercise RagRgreConvWorkflow / RagQueryPipelineConv — the classes
#    actually wired behind POST /query — so a broken wrapper or a missing
#    routing event would fail, not pass silently.
# ==============================================================================

class _StubInner:
    """Fake inner RagQueryWorkflow — returns a canned result so the conv
    integration test never touches the real RAG pipeline / DB / LLM."""
    def __init__(self, result):
        self._result = result

    def run(self, **kwargs):
        return self._handler()

    async def _handler(self):
        return self._result


@pytest.mark.asyncio
async def test_conv_workflow_emits_routing_event_before_done():
    events = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    # Patch the inner workflow to return a clean dict fast (no DB/LLM needed).
    wf._inner = _StubInner({"answer": "x", "requires_review": False})
    result = await wf.run(query="cho hỏi giá căn 2PN view biển", session_id="sess_int_routing", history=[])
    names = [e[0] for e in events]
    assert SSE_EVENT_ROUTING in names, f"routing event missing from {names}"
    routing = [d for e, d in events if e == SSE_EVENT_ROUTING][0]
    assert "intent" in routing and "conv_state" in routing
    assert "panel_hint" in routing and "lead_cta_hint" in routing


@pytest.mark.asyncio
async def test_conv_workflow_facade_returns_conv_meta():
    events = []
    wf = RagRgreConvWorkflow(on_event=lambda e, d: events.append((e, d)))
    wf._inner = _StubInner({"answer": "x", "requires_review": False})
    result = await wf.run(query="Tôi có 3 tỷ muốn tìm căn 2PN", session_id="sess_int_meta", history=[])
    assert isinstance(result, dict)
    assert "conv_state" in result
    assert "conversation_directive" in result
    # budget + bedrooms present -> afford answered -> recommend
    assert result["conv_state"] == "recommend"


@pytest.mark.asyncio
async def test_rag_query_pipeline_conv_contract():
    # The facade main.py uses must keep RagQueryPipeline's call contract.
    from api.application.pipelines.conv_workflow import RagQueryPipelineConv as Pipe
    pipe = Pipe()
    events = []
    result = await pipe.run(
        "căn 2PN giá bao nhiêu",
        session_id="sess_int_pipe",
        history=[],
        on_event=lambda e, d: events.append(e),
    )
    assert isinstance(result, dict)
    # routing event surfaced through the facade callback
    assert "routing" in events


def test_mark_phone_given_wires_to_handoff_done():
    # §6.7: POST /api/lead success -> mark_phone_given -> handoff_done.
    ctx = get_context("sess_lead_wire")
    mark_phone_given("sess_lead_wire")
    assert ctx.slots.get("phone_given") is True
    assert ctx.state == "handoff_done"
    # After handoff, CTA must never show again (gate b).
    assert maybe_lead_cta_hint(ctx) is None


# ==============================================================================
# 10. EDGE-CASE REGRESSIONS (from review): fixed bugs must not regress.
# ==============================================================================

def test_bedroom_regex_no_false_positive_on_12pn():
    # "12PN" must NOT match "2PN"; "2phans" must NOT match "2pn".
    assert extract_bedrooms("căn 12PN") is None
    assert extract_bedrooms("căn 31PN") is None
    assert extract_bedrooms("2phans") is None
    assert extract_bedrooms("căn 2PN") == "2PN"


def test_purpose_negation_not_invest():
    # "mua để ở, không đầu tư" must be stay, not invest.
    assert extract_purpose("mua để ở thật, không đầu tư") == "stay"
    assert extract_purpose("mua để đầu tư cho thuê") == "invest"


def test_lead_prefill_budget_3_5_ty():
    # §6.3: "3.5 tỷ" must not round up to "4 tỷ" (review: lossy :.0f truncation).
    note = lead_prefill_note({"budget_vnd": 3_500_000_000})
    assert "3.5 tỷ" in note
    assert "4 tỷ" not in note


def test_lead_prefill_nonpositive_budget_omitted():
    # Negative/zero budget must be omitted, never "Ngân sách: -0 triệu".
    assert "Ngân sách" not in lead_prefill_note({"budget_vnd": 0})
    assert "Ngân sách" not in lead_prefill_note({"budget_vnd": -100})


def test_get_context_none_does_not_collide():
    # None/empty session must not collapse to one shared cache key (cross-session bleed).
    a = get_context(None)
    b = get_context(None)
    assert a is not b  # each anonymous caller gets its own context
    a.slots["phone_given"] = True
    assert b.slots.get("phone_given") is None
