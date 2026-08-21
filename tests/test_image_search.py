"""Unit + integration tests for the illustrative-image search service.

Covers unit-code normalization/extraction, unit-precise re-ranking (exact ->
similar -> semantic), the direct-index rescue path, ``search_project_images``
preference/best-effort behavior, and the stable image contract. Every embedding
and DB call is faked so the suite stays fast and network-free.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import api.application.services.image_search as img


def run(coro):
    """Drive one coroutine on a fresh loop (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def _row(unit=None, unit_type=None, image_id="img-1", score=0.8, **overrides):
    """Build a DB-shaped image row; unit links via linked_subject_key, type via metadata."""
    row = {
        "image_id": image_id,
        "kind": "matbang",
        "title": f"title-{image_id}",
        "caption": "Mặt bằng căn hộ",
        "alt_text": "Mặt bằng",
        "url_cdn": "https://cdn.example/img.png",
        "width": 1200,
        "height": 900,
        "linked_subject_key": f"unit:{unit}" if unit else None,
        "metadata": {"type": unit_type} if unit_type else {},
        "score": score,
    }
    row.update(overrides)
    return row


class _FakeConn:
    """Stand-in for an asyncpg connection: returns canned rows for fetch/fetchrow."""

    def __init__(self, rows=()):
        self._rows = list(rows)

    async def fetch(self, *args, **kwargs):
        return self._rows

    async def fetchrow(self, *args, **kwargs):
        return self._rows[0] if self._rows else None


@asynccontextmanager
async def _fake_rls(rows=()):
    """Yield a fake connection instead of opening a real RLS transaction."""
    yield _FakeConn(rows)


# --- unit-code normalization / extraction -------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CH-3", "CH-03"),
        ("CH-12", "CH-12"),
        ("ch-3", "CH-03"),
        ("ch-3a", "CH-03A"),
        ("CH-03", "CH-03"),
        ("CH-9", "CH-09"),
        ("not-a-unit", None),
        ("", None),
    ],
)
def test_normalize_unit_code(raw, expected):
    assert img._normalize_unit_code(raw) == expected


def test_normalize_unit_code_does_not_collapse_ch12_and_ch12a():
    assert img._normalize_unit_code("CH-12") == "CH-12"
    assert img._normalize_unit_code("CH-12A") == "CH-12A"
    assert img._normalize_unit_code("CH-12") != img._normalize_unit_code("CH-12A")


def test_extract_unit_codes_is_case_insensitive_and_pads():
    assert img._extract_unit_codes("căn CH-3 view biển") == ["CH-03"]
    assert img._extract_unit_codes("căn ch-3a view biển") == ["CH-03A"]


def test_extract_unit_codes_no_unit_returns_empty():
    assert img._extract_unit_codes("dự án view biển 2PN") == []


def test_extract_unit_codes_does_not_conflate_ch12_and_ch12a():
    assert img._extract_unit_codes("căn CH-12 view biển") == ["CH-12"]
    assert img._extract_unit_codes("căn CH-12A view biển") == ["CH-12A"]
    assert "CH-12A" not in img._extract_unit_codes("căn CH-12 view biển")


# --- row shaping helpers ------------------------------------------------------


def test_row_to_image_contract_shape():
    row = _row(unit="CH-03", unit_type="3PN", image_id="x")
    out = img._row_to_image(row, 0.9, "exact", "Đúng căn CH-03 bạn hỏi")
    assert set(out) == {
        "image_id", "kind", "title", "caption", "alt_text", "url_cdn",
        "width", "height", "score", "match", "reason",
    }
    assert out["image_id"] == "x"
    assert out["kind"] == "matbang"
    assert out["score"] == 0.9
    assert out["match"] == "exact"
    assert out["reason"] == "Đúng căn CH-03 bạn hỏi"


def test_meta_of_parses_json_string_and_tolerates_garbage():
    assert img._meta_of({"metadata": '{"type": "3PN"}'}) == {"type": "3PN"}
    assert img._meta_of({"metadata": "not json"}) == {}
    assert img._meta_of({}) == {}


def test_row_unit_reads_linked_key_then_metadata_fallback():
    assert img._row_unit({"linked_subject_key": "unit:CH-3"}) == "CH-03"
    assert img._row_unit({"metadata": {"unit": "CH-3a"}}) == "CH-03A"
    assert img._row_unit({}) is None


# --- re-ranking ---------------------------------------------------------------


def test_rerank_orders_exact_then_similar_then_semantic(monkeypatch):
    async def no_rescue(code):
        return None

    monkeypatch.setattr(img, "_query_by_unit", no_rescue)
    scored = [
        (_row(unit="CH-12A", unit_type="2PN", image_id="semantic", score=0.9), 0.9),
        (_row(unit=None, unit_type="3PN", image_id="similar", score=0.7), 0.7),
        (_row(unit="CH-03", unit_type="3PN", image_id="exact", score=0.5), 0.5),
    ]
    out = run(img._rerank_by_unit({"CH-03"}, scored, 10))

    assert [i["image_id"] for i in out] == ["exact", "similar", "semantic"]
    assert [i["match"] for i in out] == ["exact", "similar", "semantic"]
    assert out[0]["reason"] == "Đúng căn CH-03 bạn hỏi"
    assert out[1]["reason"] == "Căn 3PN tương tự để so sánh"
    assert out[2]["reason"] is None


def test_rerank_rescues_missing_exact_with_ceiling_score(monkeypatch):
    rescued = _row(unit="CH-03", unit_type="3PN", image_id="rescued", score=0.0)

    async def rescue(code):
        assert code == "CH-03"
        return rescued

    monkeypatch.setattr(img, "_query_by_unit", rescue)
    scored = [(_row(unit=None, unit_type="3PN", image_id="semantic", score=0.8), 0.8)]
    out = run(img._rerank_by_unit({"CH-03"}, scored, 10))

    assert out[0]["image_id"] == "rescued"
    assert out[0]["match"] == "exact"
    assert out[0]["score"] == 1.0
    assert out[0]["reason"] == "Đúng căn CH-03 bạn hỏi"


def test_rerank_does_not_treat_ch12a_as_exact_for_ch12(monkeypatch):
    async def no_rescue(code):
        return None

    monkeypatch.setattr(img, "_query_by_unit", no_rescue)
    scored = [(_row(unit="CH-12A", unit_type="2PN", image_id="a", score=0.9), 0.9)]
    out = run(img._rerank_by_unit({"CH-12"}, scored, 10))

    assert out[0]["image_id"] == "a"
    assert out[0]["match"] == "semantic"


def test_rerank_honors_top_k(monkeypatch):
    async def no_rescue(code):
        return None

    monkeypatch.setattr(img, "_query_by_unit", no_rescue)
    scored = [
        (_row(unit="CH-03", unit_type="3PN", image_id="e1", score=0.9), 0.9),
        (_row(unit=None, unit_type="3PN", image_id="s1", score=0.7), 0.7),
    ]
    out = run(img._rerank_by_unit({"CH-03"}, scored, 1))
    assert len(out) == 1
    assert out[0]["image_id"] == "e1"


# --- search_images (integration: embed + DB faked) ----------------------------


async def _embed_fake(text):
    return [0.5]


def test_search_images_empty_query_returns_empty():
    assert run(img.search_images("")) == []


def test_search_images_degrades_to_empty_on_embed_error(monkeypatch):
    async def boom(text):
        raise RuntimeError("embed down")

    monkeypatch.setattr(img, "_embed_query", boom)
    assert run(img.search_images("view biển")) == []


def test_search_images_degrades_to_empty_on_db_error(monkeypatch):
    monkeypatch.setattr(img, "_embed_query", _embed_fake)

    @asynccontextmanager
    async def failing_rls():
        raise RuntimeError("db down")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(img, "with_rls_identity", failing_rls)
    assert run(img.search_images("view biển")) == []


def test_search_images_filters_below_threshold_and_keeps_semantic_order(monkeypatch):
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="high", score=0.9),
        _row(image_id="low", score=0.2),
        _row(image_id="mid", score=0.6),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("dự án view biển", top_k=4, threshold=0.4))

    assert [i["image_id"] for i in out] == ["high", "mid"]
    assert all(i["match"] == "semantic" for i in out)
    assert all(i["reason"] is None for i in out)


def test_search_images_unit_query_reranks_exact_to_head(monkeypatch):
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(unit="CH-12A", unit_type="2PN", image_id="other", score=0.9),
        _row(unit="CH-03", unit_type="3PN", image_id="target", score=0.4),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    async def no_rescue(code):
        return None

    monkeypatch.setattr(img, "_query_by_unit", no_rescue)

    out = run(img.search_images("mặt bằng căn CH-3", top_k=4, threshold=0.4))

    assert out[0]["image_id"] == "target"
    assert out[0]["match"] == "exact"
    assert out[0]["reason"] == "Đúng căn CH-03 bạn hỏi"


# --- search_project_images ----------------------------------------------------


def test_search_project_images_passes_preference_order_to_query(monkeypatch):
    captured = {}

    class _CaptureConn:
        async def fetch(self, *args, **kwargs):
            captured["args"] = args
            return []

    @asynccontextmanager
    async def rls():
        yield _CaptureConn()

    monkeypatch.setattr(img, "with_rls_identity", rls)
    out = run(img.search_project_images())

    assert out == []
    query, kind, order_str, top_k = captured["args"]
    assert kind == "matbang"
    assert order_str == "cover,render,amenity_map,amenity_collage"
    assert top_k == 6


def test_search_project_images_returns_four_shaped_images_dropping_units(monkeypatch):
    rows = [
        _row(image_id="cover", unit_type="cover"),
        _row(image_id="render", unit_type="render"),
        _row(image_id="amenity_map", unit_type="amenity_map"),
        _row(image_id="amenity_collage", unit_type="amenity_collage"),
        _row(unit="CH-03", unit_type="3PN", image_id="unitlinked"),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_project_images(top_k=4))

    assert len(out) == 4
    assert "unitlinked" not in {i["image_id"] for i in out}
    for item in out:
        assert item["match"] == "semantic"
        assert item["reason"] is None
        assert item["score"] == 1.0


def test_search_project_images_degrades_to_empty_on_db_error(monkeypatch):
    @asynccontextmanager
    async def failing():
        raise RuntimeError("db down")
        yield  # pragma: no cover - unreachable

    monkeypatch.setattr(img, "with_rls_identity", failing)
    assert run(img.search_project_images()) == []
