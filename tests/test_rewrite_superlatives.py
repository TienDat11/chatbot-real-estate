"""Unit-superlative phrasings must confirm the deterministic aggregate gate.

"rẻ nhất" / "vip nhất" extend AGGREGATE_KEYWORDS so superlative unit picks
("lấy cho anh căn rẻ nhất") reach the NL2SQL leg instead of being downgraded,
while bare "nhất" inside non-superlative words ("nhất định", "nhất trí")
stays inert. No LLM/DB — detector and _normalize_routed are pure.
"""

from api.rewrite import _normalize_routed, detect_aggregate_intent


def _route(data, query, as_of="2026-08-24"):
    return _normalize_routed(data, query, as_of)


# --- detection: the two new keywords fire on realistic user phrasing ---


def test_cheapest_superlative_detected():
    assert detect_aggregate_intent("lấy cho anh căn rẻ nhất")


def test_vip_superlative_detected():
    assert detect_aggregate_intent("cho xem studio vip nhất")


# --- routing: detector confirms NL2SQL both when LLM proposes and misses ---


def test_llm_nl2sql_confirmed_no_downgrade():
    # LLM proposes nl2sql -> detector agrees, nothing degrades.
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "nl2sql"}},
        query="lấy cho anh căn rẻ nhất",
    )
    assert routed.routing["structured_path"] == "nl2sql"
    assert "nl2sql_downgraded" not in routed.degraded
    assert "budget_injected" not in routed.degraded


def test_llm_miss_still_forces_nl2sql():
    # LLM proposes a plain spec but needs_sql -> detector forces the nl2sql leg.
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "spec"}},
        query="studio vip nhất ở dự án là căn nào",
    )
    assert routed.routing["structured_path"] == "nl2sql"
    assert "nl2sql_forced" in routed.degraded


# --- guardrail: bare "nhất" alone must never trip the gate ---


def test_bare_nhat_in_non_superlative_words_not_detected():
    for q in (
        "chị ấy nhất định phải xem căn mẫu",
        "tôi nhất trí với phương án trả thẳng",
    ):
        assert not detect_aggregate_intent(q)


def test_non_superlative_query_with_nhat_not_forced_to_nl2sql():
    # Same guardrail at routing level: LLM-proposed nl2sql gets downgraded
    # because the detector finds no superlative keyword.
    routed = _route(
        {"routing": {"needs_sql": True, "structured_path": "nl2sql"}},
        query="chị ấy nhất định phải xem căn mẫu",
    )
    assert "nl2sql_downgraded" in routed.degraded
