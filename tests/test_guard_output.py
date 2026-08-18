"""Story 4.2 — contextual disclosure + robot-phrase checks (L4, non-confidence).

Plan §3.3.2: price/estimate answers must carry a disclosure keyword ("định hướng"),
high-stakes answers must steer to "chuyên viên", normal answers need no AI line
(FE footer owns always-on); robot phrases + em-dash flag style_warn only and never
lower confidence (which stays = numeric/citation grounding).
"""

from api.domain.services.guard_output import (
    _contextual_disclosure_verdict,
    _robot_phrase_verdict,
    guard_output,
)
from api.domain.services.guard_output import (
    GuardResult,
)


def test_price_answer_with_disclosure_passes():
    v = _contextual_disclosure_verdict(
        "Giá căn A04-03 là 4 tỷ đồng, là giá định hướng, bảng hàng chính thức chuyên viên sẽ gửi kèm.",
        has_approx=False,
        high_stakes=False,
    )
    assert v["status"] == "pass"
    assert v["style_warn"] is False


def test_price_answer_missing_disclosure_fails():
    v = _contextual_disclosure_verdict(
        "Giá căn A04-03 là 4 tỷ đồng, anh chị xem thêm nhé.",
        has_approx=False,
        high_stakes=False,
    )
    assert v["status"] == "fail"
    assert "price_disclosure" in v["missing"]


def test_estimate_answer_uses_approx_keyword_passes():
    v = _contextual_disclosure_verdict(
        "Căn này ước lượng khoảng 3,9 tỷ đồng, chưa xác nhận chính thức.",
        has_approx=True,
        high_stakes=False,
    )
    assert v["status"] == "pass"


def test_high_stakes_answer_with_specialist_passes():
    v = _contextual_disclosure_verdict(
        "Cầm cố QSDĐ không được Luật ghi nhận, khuyến nghị xác nhận với chuyên viên pháp lý.",
        has_approx=False,
        high_stakes=True,
    )
    assert v["status"] == "pass"
    assert v["disclosure_scope"] == "high_stakes"


def test_high_stakes_answer_missing_specialist_fails():
    v = _contextual_disclosure_verdict(
        "Cầm cố QSDĐ không được Luật ghi nhận, rủi ro vô hiệu.",
        has_approx=False,
        high_stakes=True,
    )
    assert v["status"] == "fail"
    assert "specialist_steer" in v["missing"]


def test_normal_greeting_needs_no_disclosure():
    v = _contextual_disclosure_verdict(
        "Chào anh chị, dự án nằm ngay giao lộ Lê Văn Lương - Lê Đức Thọ, gần biển Sơn Trà.",
        has_approx=False,
        high_stakes=False,
    )
    assert v["status"] == "pass"
    assert v["disclosure_scope"] == "none"


def test_robot_phrases_flag_style_warn_only():
    r = _robot_phrase_verdict("Dựa trên thông tin được cung cấp, căn này rộng 68 m2.")
    assert r["status"] == "warn"
    assert r["style_warn"] is True
    assert "dựa trên thông tin được cung cấp" in r["robot_phrases"]


def test_em_dash_flags_style_warn():
    r = _robot_phrase_verdict("Căn này rộng 68 m2 — anh chị xem thêm nhé.")
    assert r["status"] == "warn"
    assert r["style_warn"] is True
    assert r["em_dash"] is True


def test_clean_answer_has_no_style_warn():
    r = _robot_phrase_verdict("Căn này rộng 68 m2, anh chị xem thêm nhé.")
    assert r["status"] == "pass"
    assert r["style_warn"] is False


def test_guard_output_includes_disclosure_and_robot_verdicts():
    """The full guard pipeline carries the two style verdicts and keeps confidence."""
    import asyncio

    res = asyncio.run(
        guard_output(
            "Giá căn A04-03 là 4 tỷ đồng, là giá định hướng, bảng hàng chính thức sẽ gửi kèm.",
            facts=[],
            sources=[],
            routing={},
        )
    )
    assert res.verdicts["disclosure"]["status"] == "pass"
    assert "robot_phrase" in res.verdicts
    assert res.verdicts["robot_phrase"]["style_warn"] is False
    assert res.confidence in ("HIGH", "MEDIUM", "LOW")
