"""Story 4.4 — Query understanding v2: colloquial synonyms (R7).

`api/domain/services/synonyms.py` maps colloquial phrasings (người 45-70) to
canonical sales keywords, and `_normalize_routed` enriches `hl_keywords` with
them (ADD only — never replaces the LLM output). Pure, no LLM/DB.

Covers: the SALES_SYNONYMS mapping, the enrichment function, and the
integration point in `_normalize_routed` (colloquial query -> canonical
hl_keywords present in the RoutedResult).
"""

from api.domain.services.synonyms import SALES_SYNONYMS, enrich_hl_keywords
from api.domain.services.rewrite import _normalize_routed


def _route(data, query="4 tỷ mua nhà nào", as_of="2026-08-16"):
    return _normalize_routed(data, query, as_of)


# --- the mapping itself (colloquial -> canonical) ---


def test_synonyms_mapping_shape():
    # Every key is a colloquial phrase, every value is a non-empty tuple of
    # canonical keywords.
    assert isinstance(SALES_SYNONYMS, dict)
    for key, canon in SALES_SYNONYMS.items():
        assert isinstance(key, str) and key
        assert isinstance(canon, tuple) and len(canon) >= 1
        assert all(isinstance(c, str) and c for c in canon)


def test_synonyms_bedroom_mapping():
    assert "2PN" in SALES_SYNONYMS["nhà 2 ngủ"]
    assert "2PN" in SALES_SYNONYMS["2 phòng ngủ"]
    assert "Studio" in SALES_SYNONYMS["studio"]
    assert "1.5PN" in SALES_SYNONYMS["1 ngủ rưỡi"]


def test_synonyms_view_mapping():
    assert "view biển" in SALES_SYNONYMS["biển"]
    assert "view biển" in SALES_SYNONYMS["hướng biển"]
    assert "căn góc" in SALES_SYNONYMS["căn góc"]
    assert "nội khu" in SALES_SYNONYMS["nội khu"]


def test_synonyms_payment_mapping():
    assert "HTLS" in SALES_SYNONYMS["trả chậm"]
    assert "HTLS" in SALES_SYNONYMS["trả góp 0%"]
    assert "sớm 95" in SALES_SYNONYMS["đóng sớm"]
    assert "thanh toán thảnh thơi" in SALES_SYNONYMS["thảnh thơi"]
    assert "chiết khấu" in SALES_SYNONYMS["giảm giá"]
    assert "early booking" in SALES_SYNONYMS["eb"]
    assert "tiền cọc" in SALES_SYNONYMS["cọc"]


# --- enrichment function (pure) ---


def test_enrich_adds_canonical_keywords():
    out = enrich_hl_keywords("cho hỏi nhà 2 ngủ hướng biển còn không", [])
    assert "2PN" in out
    assert "view biển" in out


def test_enrich_does_not_replace_existing():
    out = enrich_hl_keywords("nhà 2 ngủ", ["giá"])
    assert "giá" in out  # LLM keyword preserved
    assert "2PN" in out  # canonical added


def test_enrich_deduplicates():
    out = enrich_hl_keywords("nhà 2 ngủ 2 phòng ngủ", ["2PN"])
    assert out.count("2PN") == 1


def test_enrich_no_match_returns_unchanged():
    assert enrich_hl_keywords("cảm ơn bạn", ["x"]) == ["x"]


def test_enrich_case_insensitive():
    out = enrich_hl_keywords("STUDIO view biển", [])
    assert "Studio" in out
    assert "view biển" in out


def test_enrich_empty_query():
    assert enrich_hl_keywords("", []) == []


def test_enrich_ascii_abbrev_word_boundary():
    # "ck"/"eb"/"htls" must not fire inside unrelated words ("check", "web").
    assert enrich_hl_keywords("tôi muốn check giá", []) == []
    assert enrich_hl_keywords("xem trên web", []) == []
    assert enrich_hl_keywords("căn này ck 4%", []) == ["chiết khấu"]
    assert enrich_hl_keywords("eb 3% cộng dồn", []) == ["early booking"]


# --- integration: _normalize_routed enriches hl_keywords (R7) ---


def test_normalize_routed_enriches_colloquial_bedroom():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}, "hl_keywords": []},
        query="nhà 2 ngủ còn không",
    )
    assert "2PN" in routed.hl_keywords


def test_normalize_routed_enriches_colloquial_view():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}, "hl_keywords": []},
        query="căn hộ hướng biển giá bao nhiêu",
    )
    assert "view biển" in routed.hl_keywords


def test_normalize_routed_enriches_htls_not_high_stakes():
    # "trả góp 0% là sao" is a financing question, NOT high-stakes (plan §5).
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}, "hl_keywords": []},
        query="trả góp 0% là sao, có bị lừa hông",
    )
    assert "HTLS" in routed.hl_keywords
    assert routed.high_stakes is False


def test_normalize_routed_enriches_payment_synonyms():
    routed = _route(
        {"routing": {"needs_sql": False, "structured_path": "none"}, "hl_keywords": []},
        query="trả chậm thế nào",
    )
    assert "HTLS" in routed.hl_keywords
