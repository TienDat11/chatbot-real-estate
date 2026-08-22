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


def test_search_images_filters_below_threshold_and_margin(monkeypatch):
    """Floor rejects weak scores; the same-kind gate drops a far tail.

    Top hit is 0.9: the 0.6 candidate sits 0.3 below it, so the same-kind margin
    (default 0.15) removes it — that is the "irrelevant 4th image" the bug report
    described. The 0.2 candidate never passes the absolute floor either.
    """
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="high", score=0.9),
        _row(image_id="low", score=0.2),
        _row(image_id="mid", score=0.6),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("dự án view biển", top_k=4, threshold=0.4))

    assert [i["image_id"] for i in out] == ["high"]
    assert all(i["match"] == "semantic" for i in out)
    assert all(i["reason"] is None for i in out)


def test_search_images_keeps_candidates_within_margin_of_top(monkeypatch):
    """A tight topical cluster (scores close to the top) must all be kept."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="high", score=0.9),
        _row(image_id="close", score=0.88),
        _row(image_id="tail", score=0.85),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("dự án view biển", top_k=4, threshold=0.4))

    assert [i["image_id"] for i in out] == ["high", "close", "tail"]


def test_search_images_drops_unrelated_tail_for_payment_query(monkeypatch):
    """Payment query must not trail into a floor-plan image that only shares project
    vocabulary — the exact regression reported: top payment hit 0.62, unrelated
    floor plan 0.51 (gap 0.11 > cross_kind_margin 0.05) must be dropped, not attached.
    """
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="payment-1", kind="thanh-toan", score=0.62),
        _row(image_id="payment-2", kind="thanh-toan", score=0.6),
        _row(image_id="payment-3", kind="thanh-toan", score=0.58),
        _row(image_id="matbang-tail", kind="matbang", score=0.51),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("phương thức thanh toán mua căn hộ camellia", top_k=4))

    assert [i["image_id"] for i in out] == ["payment-1", "payment-2", "payment-3"]
    assert all(i["kind"] == "thanh-toan" for i in out)


def test_search_images_payment_query_keeps_all_four_same_kind_images(monkeypatch):
    """THE regression this fix addresses: all four payment-method images come back.

    Measured scores for the payment question (same-kind cluster): 0.5615 / 0.5520
    / 0.5231 / 0.4586. The old scalar margin (0.07) dropped the 4th (htls, gap
    0.1029 > 0.07); same_kind_margin (0.15) holds the whole cluster and every
    member still clears the 0.45 floor (0.4586 > 0.45).
    """
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="thanh-thoi", kind="thanh-toan", score=0.5615),
        _row(image_id="chuan", kind="thanh-toan", score=0.5520),
        _row(image_id="som-95", kind="thanh-toan", score=0.5231),
        _row(image_id="htls", kind="thanh-toan", score=0.4586),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("phương thức thanh toán mua căn hộ camellia", top_k=4))

    assert [i["image_id"] for i in out] == ["thanh-thoi", "chuan", "som-95", "htls"]
    assert all(i["kind"] == "thanh-toan" for i in out)
    assert all(i["match"] == "semantic" for i in out)


def test_search_images_cross_kind_tail_dropped_beyond_cross_margin(monkeypatch):
    """A different-kind image close in score is still rejected by the tight window.

    top thanh-toan 0.62 with a matbang 0.51 behind it (gap 0.11) exceeds
    cross_kind_margin (0.05), so only the payment image survives — the exact
    "hỏi thanh toán ra ảnh mặt bằng" bug.
    """
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="payment-1", kind="thanh-toan", score=0.62),
        _row(image_id="matbang-tail", kind="matbang", score=0.51),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("phương thức thanh toán mua căn hộ camellia", top_k=4))

    assert [i["image_id"] for i in out] == ["payment-1"]


def test_search_images_returns_nothing_when_no_relevant_image(monkeypatch):
    """No match above the floor -> no image at all, never a best-effort floor plan."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="weak-1", score=0.44),
        _row(image_id="weak-2", score=0.41),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    out = run(img.search_images("tổng quan thị trường bất động sản Đà Nẵng"))

    assert out == []


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


# --- search_images: boundary gates (threshold / kind-aware margin / top_k / pool) --------

# The floor/margin comparisons are strict: a candidate exactly AT a bound must be
# kept, and any value below it by even float noise (1e-9) must be dropped. These
# tests pin that the comparisons use `<`/`>` against float scores and never
# introduce an epsilon tolerance that would let a barely-off-topic tail through.


def test_search_images_score_exactly_at_threshold_is_kept(monkeypatch):
    """Score == threshold survives the floor (`float(score) < threshold` is strict)."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(
        img, "with_rls_identity", lambda: _fake_rls([_row(image_id="edge", score=0.45)])
    )
    out = run(img.search_images("dự án view biển", threshold=0.45))
    assert [i["image_id"] for i in out] == ["edge"]


def test_search_images_score_below_threshold_by_epsilon_is_dropped(monkeypatch):
    """A score 1e-9 under the floor is still rejected — no rounding rescue."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(
        img,
        "with_rls_identity",
        lambda: _fake_rls([_row(image_id="weak", score=0.45 - 1e-9)]),
    )
    out = run(img.search_images("dự án view biển", threshold=0.45))
    assert out == []


def test_search_images_same_kind_gap_exactly_at_margin_is_kept(monkeypatch):
    """`top_score - s <= same_kind_margin` is inclusive: a same-kind distance
    exactly == the margin is kept. Float note: 0.7 - 0.55 computes to
    0.1499999999999999 (≤ 0.15), so this pair lands ON the boundary, not over it
    (0.9 - 0.75 would compute to 0.15000000000000002 and be correctly dropped)."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="top", score=0.7),
        _row(image_id="edge", score=0.55),  # computed gap 0.1499999999999999
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4, same_kind_margin=0.15))
    assert [i["image_id"] for i in out] == ["top", "edge"]


def test_search_images_same_kind_gap_beyond_margin_by_epsilon_is_dropped(monkeypatch):
    """A same-kind gap margin + 1e-9 exceeds the relative gate and must be dropped."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="top", score=0.9),
        _row(image_id="edge", score=0.9 - 0.15 - 1e-9),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4, same_kind_margin=0.15))
    assert [i["image_id"] for i in out] == ["top"]


def test_search_images_cross_kind_gap_exactly_at_margin_is_kept(monkeypatch):
    """`top_score - s <= cross_kind_margin` is inclusive: a different-kind distance
    exactly == the margin is kept. Float note: 0.5 - 0.45 computes to
    0.04999999999999999 (≤ 0.05), so this pair lands ON the boundary, not over it."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="top", kind="thanh-toan", score=0.5),
        _row(image_id="edge", kind="matbang", score=0.45),  # computed gap 0.04999999999999999
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4, cross_kind_margin=0.05))
    assert [i["image_id"] for i in out] == ["top", "edge"]


def test_search_images_cross_kind_gap_beyond_margin_by_epsilon_is_dropped(monkeypatch):
    """A cross-kind gap margin + 1e-9 exceeds the relative gate and must be dropped."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="top", kind="thanh-toan", score=0.9),
        _row(image_id="edge", kind="matbang", score=0.9 - 0.05 - 1e-9),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4, cross_kind_margin=0.05))
    assert [i["image_id"] for i in out] == ["top"]


def test_search_images_legacy_margin_overrides_both_windows(monkeypatch):
    """Back-compat: passing the old scalar ``margin`` drives both windows with one
    value, so a caller that relied on the pre-fix gate keeps old behavior — a
    same-kind candidate 0.1 below the top is dropped by margin=0.07 even though
    the same_kind_margin default (0.15) would keep it."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="top", kind="thanh-toan", score=0.9),
        _row(image_id="near", kind="thanh-toan", score=0.8),  # gap 0.1 > 0.07
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4, margin=0.07))
    assert [i["image_id"] for i in out] == ["top"]


def test_search_images_kind_aware_windows_apply_independently(monkeypatch):
    """The core of the fix: the same 0.1 gap is kept for a same-kind candidate but
    dropped for a different-kind one, in a single query."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="top", kind="thanh-toan", score=0.9),
        _row(image_id="same-kind", kind="thanh-toan", score=0.8),  # gap 0.1 < 0.15
        _row(image_id="cross-kind", kind="matbang", score=0.8),  # gap 0.1 > 0.05
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4))
    assert [i["image_id"] for i in out] == ["top", "same-kind"]


def test_search_images_top_k_slices_after_margin_gate(monkeypatch):
    """The relative gate runs BEFORE the top_k slice: with 5 candidates all inside
    the margin, exactly the best 2 survive — the pool is not the final count."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [_row(image_id=f"r{i}", score=0.9 - 0.01 * i) for i in range(5)]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", top_k=2, threshold=0.4))
    assert [i["image_id"] for i in out] == ["r0", "r1"]


def test_search_images_fetch_pool_is_max_top_k_8(monkeypatch):
    """The vector pass requests a superset pool so the gates are not starved by
    LIMIT: pool = max(top_k, 8). Capturing the fetch args proves the LIMIT binds
    the pool, not the final count — a topical cluster just outside the raw top_k
    neighbors still gets a chance to pass the gates."""
    calls = []

    class _CaptureConn:
        async def fetch(self, *args, **kwargs):
            calls.append(args)
            return []

    @asynccontextmanager
    async def rls():
        yield _CaptureConn()

    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(img, "with_rls_identity", rls)

    run(img.search_images("dự án view biển", top_k=2))
    assert calls[0][2] == 8  # max(2, 8)

    calls.clear()
    run(img.search_images("dự án view biển", top_k=10))
    assert calls[0][2] == 10  # top_k above the pool floor keeps its own size


def test_search_images_all_rows_below_floor_returns_empty(monkeypatch):
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(
        img,
        "with_rls_identity",
        lambda: _fake_rls([_row(image_id="a", score=0.44), _row(image_id="b", score=0.41)]),
    )
    assert run(img.search_images("tổng quan thị trường bất động sản")) == []


def test_search_images_no_rows_returns_empty(monkeypatch):
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls([]))
    assert run(img.search_images("dự án view biển")) == []


def test_search_images_single_row_through_gate_returns_it(monkeypatch):
    """A lone candidate above the floor (and trivially within its own margin) is
    returned — the gates must not over-trim a 1-item result."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(
        img, "with_rls_identity", lambda: _fake_rls([_row(image_id="solo", score=0.7)])
    )
    out = run(img.search_images("dự án view biển", threshold=0.4))
    assert [i["image_id"] for i in out] == ["solo"]


def test_search_images_tied_top_scores_both_kept_in_score_desc_order(monkeypatch):
    """A score tie at the top must keep BOTH candidates; the returned order is the
    stable raw fetch order (score desc), never a drop-one-arbitrarily tie-break."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(image_id="first", score=0.9),
        _row(image_id="second", score=0.9),
        _row(image_id="tail", score=0.7),  # gap 0.2 > same_kind_margin 0.15 -> dropped
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))
    out = run(img.search_images("dự án view biển", threshold=0.4))
    assert [i["image_id"] for i in out] == ["first", "second"]


def test_search_images_unit_query_skips_margin_gate(monkeypatch):
    """When the query names a unit code the relative margin gate is BYPASSED: a
    row far below the top (gap 0.3 > same_kind_margin 0.15) still enters the
    re-rank branch because it may be the same-type visual the re-ranker wants to keep."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(unit="CH-03", unit_type="3PN", image_id="exact", score=0.9),
        _row(unit=None, unit_type="2PN", image_id="far", score=0.6),
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    async def no_rescue(code):
        return None

    monkeypatch.setattr(img, "_query_by_unit", no_rescue)
    out = run(img.search_images("mặt bằng căn CH-3", top_k=4, threshold=0.4))

    assert [i["image_id"] for i in out] == ["exact", "far"]


def test_search_images_unit_rescue_bypasses_floor(monkeypatch):
    """Exact rescue deliberately bypasses the absolute floor: the vector pass may
    score the asked-for unit below threshold, but the direct-index hit still
    surfaces it at the 1.0 ceiling (its own low raw score is irrelevant)."""
    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    rows = [
        _row(unit="CH-12A", unit_type="2PN", image_id="other", score=0.9),
        _row(unit="CH-03", unit_type="3PN", image_id="target-vec", score=0.3),  # below floor
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(rows))

    rescued = _row(unit="CH-03", unit_type="3PN", image_id="target", score=0.0)  # low raw score

    async def rescue(code):
        assert code == "CH-03"
        return rescued

    monkeypatch.setattr(img, "_query_by_unit", rescue)
    out = run(img.search_images("mặt bằng căn CH-3", top_k=4, threshold=0.4))

    assert out[0]["image_id"] == "target"
    assert out[0]["score"] == 1.0
    assert out[0]["match"] == "exact"


def test_search_images_empty_query_short_circuits_without_calls(monkeypatch):
    """An empty query must return [] BEFORE any embed or DB call (spy proves it)."""
    calls = {"embed": 0, "db": 0}

    async def spy_embed(text):
        calls["embed"] += 1
        return [0.5]

    @asynccontextmanager
    async def spy_rls():
        calls["db"] += 1
        yield _FakeConn()

    monkeypatch.setattr(img, "_embed_query", spy_embed)
    monkeypatch.setattr(img, "with_rls_identity", spy_rls)

    assert run(img.search_images("")) == []
    assert calls == {"embed": 0, "db": 0}


def test_search_images_whitespace_query_still_queries_but_returns_empty(monkeypatch):
    """REPORTED GAP: only the empty string short-circuits (image_search.py line
    ~293); a whitespace-only query still runs the embed + DB fetch. We lock the
    ACTUAL behavior (whitespace hits the pipeline and yields [] only because no
    row passes the gates) so the discrepancy stays visible in the suite. A fix
    short-circuiting whitespace would flip the embed/db call counts and update
    this test."""
    calls = {"embed": 0, "db": 0}

    async def spy_embed(text):
        calls["embed"] += 1
        return [0.5]

    @asynccontextmanager
    async def spy_rls():
        calls["db"] += 1
        yield _FakeConn()

    monkeypatch.setattr(img, "_embed_query", spy_embed)
    monkeypatch.setattr(img, "with_rls_identity", spy_rls)

    assert run(img.search_images("   ")) == []
    assert calls == {"embed": 1, "db": 1}


# --- IMAGE_QUERY SQL contract -------------------------------------------------

# Each assertion below pins a semantically load-bearing fragment of IMAGE_QUERY so
# an accidental SQL/schema edit is caught at test time. A fragment is load-bearing
# when changing it silently changes what images the pipeline can surface.


def test_image_query_sql_contract_kind_filter():
    """The kind filter must keep excluding legal/Q&A artifacts: phaply/qna images
    exist in the DB but must never illustrate an answer (policy images are shown
    elsewhere, and surfacing legal artifacts would confuse the user)."""
    assert "i.kind NOT IN ('phaply', 'qna')" in img.IMAGE_QUERY


def test_image_query_sql_contract_cosine_operator():
    """The pgvector `<=>` cosine-distance operator must drive both the score
    expression and the ORDER BY. Switching metric (e.g. to L2 <->) silently
    redefines what "similar caption" means and would break the tuned 0.45 floor."""
    assert img.IMAGE_QUERY.count("<=>") == 2
    assert "1 - (e.embedding <=> $1::vector) AS score" in img.IMAGE_QUERY
    assert "ORDER BY e.embedding <=> $1::vector" in img.IMAGE_QUERY


def test_image_query_sql_contract_limit_param():
    """LIMIT $2 binds the pool size (max(top_k, 8)) passed by the caller. A
    hard-coded LIMIT would starve the floor/margin gates, which need the superset
    pool to choose from before the final top_k slice."""
    assert "LIMIT $2" in img.IMAGE_QUERY


def test_image_query_sql_contract_server_side_vector_cast():
    """$1 arrives as a '[0.1, 0.2, ...]' text literal and is cast to vector
    server-side; dropping ::vector makes asyncpg try to bind a bare float list
    and the query fails at encode time."""
    assert "$1::vector" in img.IMAGE_QUERY


def test_image_query_sql_contract_published_gate():
    """Only published images may illustrate answers; drafts/archived rows must be
    invisible to the gallery."""
    assert "i.status = 'published'" in img.IMAGE_QUERY


def test_search_images_fetch_args_are_vector_literal_and_pool(monkeypatch):
    """fetch must receive exactly (query, vec_literal, pool): the vector literal is
    a '[...]' string (asyncpg cannot encode a bare float list for a vector column)
    and the pool is an int — the two args a refactor could silently swap."""
    captured = {}

    class _CaptureConn:
        async def fetch(self, *args, **kwargs):
            captured["args"] = args
            return [_row(image_id="a", score=0.8)]

    @asynccontextmanager
    async def rls():
        yield _CaptureConn()

    monkeypatch.setattr(img, "_embed_query", _embed_fake)
    monkeypatch.setattr(img, "with_rls_identity", rls)
    out = run(img.search_images("dự án view biển", top_k=4))

    query, vec_literal, pool = captured["args"]
    assert query == img.IMAGE_QUERY
    assert vec_literal == "[0.5]"  # _embed_fake -> [0.5] -> server-side literal
    assert isinstance(pool, int) and pool == 8
    assert out[0]["image_id"] == "a"
