"""Unit tests for `api.route_intent` classifier (Story 4.1, ADR-0002 D1/D3).

Covers: politeness/closure handling (FIX-7 — must not drop a real follow-up),
the four short-circuit intents (HANDOFF/COMPANY/LOCATION/CLOSURE), PRICE/LEGAL
fall-through, and no false-handoff on benign small-talk. Pure — no network.
"""

from api.route_intent import (
    ClassifyResult,
    Intent,
    classify_intent,
)


def _cls(q: str, history: list[dict] | None = None) -> ClassifyResult:
    return classify_intent(q, history)


# --- primary intent matches ---


def test_handoff_phone_requests():
    assert _cls("cho tôi số điện thoại tư vấn").intent == Intent.HANDOFF
    assert _cls("gọi lại cho tôi nhé").intent == Intent.HANDOFF
    assert _cls("tôi muốn hẹn xem nhà").intent == Intent.HANDOFF
    assert _cls("gửi bảng giá báo em nhé").intent == Intent.HANDOFF


def test_handoff_purchase_signals():
    assert _cls("tôi muốn đặt cọc căn studio").intent == Intent.HANDOFF
    assert _cls("ký hợp đồng được chưa").intent == Intent.HANDOFF
    assert _cls("cho gặp nhân viên kinh doanh").intent == Intent.HANDOFF


def test_company_intent():
    assert _cls("chủ đầu tư dự án là ai").intent == Intent.COMPANY
    assert _cls("công ty địa ốc thành lâm là của ai").intent == Intent.COMPANY
    assert _cls("camellia là của công ty nào").intent == Intent.COMPANY


def test_location_intent():
    assert _cls("dự án ở đâu").intent == Intent.LOCATION
    assert _cls("vị trí dự án thế nào").intent == Intent.LOCATION
    assert _cls("gần chợ có gì không").intent == Intent.LOCATION


def test_price_intent():
    assert _cls("giá căn studio bao nhiêu").intent == Intent.PRICE
    assert _cls("tôi có 4 tỷ mua nhà nào").intent == Intent.PRICE
    assert _cls("trả góp bao nhiêu 1 tháng").intent == Intent.PRICE
    assert _cls("chiết khấu thanh toán sớm thế nào").intent == Intent.PRICE


def test_legal_intent():
    assert _cls("quy hoạch khu này thế nào").intent == Intent.LEGAL
    assert _cls("thế chấp đất được không").intent == Intent.LEGAL
    assert _cls("chuyển nhượng cần công chứng không").intent == Intent.LEGAL


def test_other_falls_through():
    assert _cls("bạn tên gì").intent == Intent.OTHER
    assert _cls("có tin gì mới về dự án không").intent == Intent.OTHER


# --- FIX-7: politeness/closure — never drop a real follow-up ---


def test_politeness_thanks_is_closure():
    assert _cls("cảm ơn bạn").intent == Intent.CLOSURE
    assert _cls("ok được rồi").intent == Intent.CLOSURE
    assert _cls("ừ thế thôi").intent == Intent.CLOSURE


def test_politeness_with_question_stays_open():
    # "cảm ơn... giá bao nhiêu" must NOT be dropped (FIX-7).
    assert _cls("cảm ơn bạn. cho hỏi giá căn studio?").intent == Intent.PRICE
    assert _cls("ok. thế còn căn 2PN thì sao?").intent != Intent.CLOSURE
    assert _cls("cảm ơn. bao nhiêu vốn để mua 1PN").intent == Intent.PRICE
    assert _cls("ok rồi. còn vị trí thì sao").intent != Intent.CLOSURE


def test_politeness_with_history_followup_stays_open():
    # Follow-up question continues the turn even though the reply is polite.
    history = [{"role": "user", "content": "giá căn studio bao nhiêu?"}]
    assert _cls("cảm ơn", history).intent != Intent.CLOSURE
    assert _cls("ok", history).intent != Intent.CLOSURE


# --- no false-handoff on benign small-talk (Story 4.1 gate) ---


def test_benign_smalltalk_not_handoff():
    candidates = [
        "xin chào",
        "hello",
        "bạn khỏe không",
        "dạ vâng biết rồi",
        "tôi sẽ xem lại",
        "câu hỏi này khó quá",
        "ok bạn nói tiếp đi",
        "cho tôi thời gian suy nghĩ",
    ]
    for q in candidates:
        assert _cls(q).intent != Intent.HANDOFF, q
        assert _cls(q).intent != Intent.COMPANY, q


def test_specific_unit_question_is_price_not_handoff():
    # Unit-information questions fall through the LLM router (never handoff).
    assert _cls("căn CH-10 giá bao nhiêu").intent == Intent.PRICE
    assert _cls("căn hộ 2PN view nội khu thế nào").intent not in (
        Intent.HANDOFF, Intent.COMPANY, Intent.LOCATION, Intent.CLOSURE,
    )
    assert _cls("cho tôi xem giá căn góc").intent == Intent.PRICE


# --- determinism / empty inputs ---


def test_empty_or_whitespace_other():
    assert _cls("").intent == Intent.OTHER
    assert _cls("   ").intent == Intent.OTHER


def test_date_like_and_amount_numbers_not_handoff():
    assert _cls("năm 2025 quy định thế nào").intent != Intent.HANDOFF
    assert _cls("chi phí khoảng 100 triệu đủ không").intent == Intent.PRICE
