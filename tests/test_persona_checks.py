"""Story 4.7 — unit tests for `_persona_checks` regex/structure rules.

Covers: citation presence/absence, em-dash, robot phrases, question counting,
newly-added disclosure_type and cta_allowed checks. Pure — no pipeline, no LLM.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.run_eval import _persona_checks  # noqa: E402


def test_pass_all():
    expect = {
        "has_direct_answer": True, "has_citation": True, "next_step_questions": 1,
        "no_em_dash": True, "no_robot_phrase": True, "disclosure_type": "price", "cta_allowed": False,
    }
    answer = "Giá định hướng căn 2PN là X tỷ [fe-001]. Anh/chị để em biết ngân sách nhé?"
    assert _persona_checks(answer, expect) == []


def test_missing_citation():
    expect = {"has_citation": True}
    assert any("has_citation" in f for f in _persona_checks("câu trả lời không citation", expect))


def test_refusal_no_citation():
    expect = {"has_citation": False}
    assert any("citation" in f for f in _persona_checks("bạn hỏi ngoài phạm vi [fe-001] nhé", expect))


def test_em_dash_detected():
    expect = {"no_em_dash": True}
    assert any("em-dash" in f for f in _persona_checks("giá — định hướng", expect))


def test_robot_phrase_detected():
    expect = {"no_robot_phrase": True}
    assert any("robot_phrase" in f for f in _persona_checks("Dựa trên thông tin được cung cấp, ...", expect))


def test_question_count_zero():
    expect = {"next_step_questions": 0}
    assert _persona_checks("trả lời dứt khoát không hỏi", expect) == []


def test_question_count_one():
    expect = {"next_step_questions": 1}
    assert _persona_checks("trả lời. Anh quan tâm loại nào ạ?", expect) == []


def test_question_count_two_fails_for_one():
    expect = {"next_step_questions": 1}
    assert any("next_step" in f for f in _persona_checks("hỏi a? b?", expect))


def test_percent_sign_not_a_question():
    # "trả góp 0%" / "?0%" must not count as a question
    expect = {"next_step_questions": 0}
    assert _persona_checks("chính sách trả góp 0% áp dụng hiệu lực [fe-001]", expect) == []


def test_disclosure_marker_required():
    expect = {"disclosure_type": "price"}
    assert any("disclosure_type" in f for f in _persona_checks("căn này [fe-001]", expect))


def test_disclosure_none_rejects_marker():
    expect = {"disclosure_type": "none"}
    assert any("disclosure_type" in f for f in _persona_checks("giá định hướng X [fe-001]", expect))


def test_cta_allowed_true_requires_invite():
    expect = {"cta_allowed": True}
    assert any("cta_allowed" in f for f in _persona_checks("đây là thông tin cho anh [fe-001]", expect))


def test_cta_allowed_false_rejects_hard_close():
    expect = {"cta_allowed": False}
    assert any("cta_allowed" in f for f in _persona_checks("anh ký ngay hôm nay nhé [fe-001]", expect))
