"""Rewrite-guard contract for the CDN origin backfill (ISSUE-3 / FR-13/FR-14).

These cases are pure (no network, no DB): they pin the fail-closed guards that
stop double-prefix regressions and prove healthy rows are never touched.
"""

from __future__ import annotations

from scripts.migrate_cdn_origins import (
    build_canonical_url,
    parse_media_components,
    plan_rows,
)

MAPPED_ORIGIN = "https://pub-healthy.r2.test"
BROKEN_ORIGIN = "https://pub-broken.r2.test"


def test_parse_accepts_absolute_https_url() -> None:
    components = parse_media_components(f"{BROKEN_ORIGIN}/images/camellia/matbang/a.png")
    assert components is not None
    assert components.origin == BROKEN_ORIGIN
    assert components.object_key == "images/camellia/matbang/a.png"


def test_parse_rejects_relative_key_and_non_http() -> None:
    assert parse_media_components("images/camellia/matbang/a.png") is None
    assert parse_media_components("ftp://host/a.png") is None
    assert parse_media_components("https://user:pw@host/a.png") is None


def test_build_from_relative_key_is_exact_join() -> None:
    assert (
        build_canonical_url(MAPPED_ORIGIN, "images/camellia/matbang/a.png")
        == f"{MAPPED_ORIGIN}/images/camellia/matbang/a.png"
    )


def test_build_refuses_absolute_url_as_object_key() -> None:
    # Feeding an absolute URL where a key belongs would create a double prefix.
    assert build_canonical_url(MAPPED_ORIGIN, "https://evil.test/a.png") is None
    assert build_canonical_url(MAPPED_ORIGIN, "http://evil.test/a.png") is None


def test_build_refuses_double_prefix_inputs() -> None:
    assert (
        build_canonical_url(MAPPED_ORIGIN, f"{MAPPED_ORIGIN}/images/a.png")
        is None
    )
    # Origin itself carrying a path/query/credential is not a bare authority.
    assert build_canonical_url(f"{MAPPED_ORIGIN}/images", "a.png") is None
    assert build_canonical_url("not-a-url", "a.png") is None
    # Traversal must never survive construction.
    assert build_canonical_url(MAPPED_ORIGIN, "images/../secrets.png") is None


def test_unmapped_project_row_is_manual_review_only() -> None:
    rows = [{"image_id": "i1", "project_key": "soleil", "status": "published",
             "url_cdn": f"{BROKEN_ORIGIN}/images/soleil/matbang/a.png"}]
    plans = plan_rows(rows, chosen_origins={"soleil": None}, url_health={})
    assert plans[0].decision == "manual_review"
    assert "no healthy mapped origin" in plans[0].reason
    assert plans[0].new_url is None


def test_live_200_row_on_healthy_origin_is_never_rewritten() -> None:
    url = f"{MAPPED_ORIGIN}/images/camellia/matbang/a.png"
    rows = [{"image_id": "i1", "project_key": "camellia", "status": "published",
             "url_cdn": url}]
    plans = plan_rows(
        rows,
        chosen_origins={"camellia": MAPPED_ORIGIN},
        url_health={url: 200},
    )
    assert plans[0].decision == "keep_healthy"
    assert plans[0].new_url is None


def test_broken_origin_row_with_verified_candidate_plans_rewrite() -> None:
    old = f"{BROKEN_ORIGIN}/images/camellia/matbang/a.png"
    new = f"{MAPPED_ORIGIN}/images/camellia/matbang/a.png"
    rows = [{"image_id": "i1", "project_key": "camellia", "status": "published",
             "url_cdn": old}]
    plans = plan_rows(
        rows,
        chosen_origins={"camellia": MAPPED_ORIGIN},
        url_health={old: 401, new: 200},
    )
    assert plans[0].decision == "rewrite"
    assert plans[0].new_url == new
    assert plans[0].new_url.count("://") == 1


def test_candidate_without_200_evidence_never_rewrites() -> None:
    old = f"{BROKEN_ORIGIN}/images/camellia/matbang/a.png"
    rows = [{"image_id": "i1", "project_key": "camellia", "status": "published",
             "url_cdn": old}]
    plans = plan_rows(
        rows,
        chosen_origins={"camellia": MAPPED_ORIGIN},
        url_health={old: 401},  # candidate never observed 200
    )
    assert plans[0].decision == "manual_review"


def test_chosen_origin_requires_verified_object_on_mapped_origin() -> None:
    health = {
        f"{BROKEN_ORIGIN}/images/camellia/matbang/a.png": 401,
        f"{MAPPED_ORIGIN}/images/camellia/matbang/a.png": 200,
        # soleil's healthy object lives on its own origin.
        "https://soleil-origin.r2.test/images/soleil/matbang/b.png": 200,
    }
    chosen = _choose_with_map(health)
    assert chosen["camellia"] == MAPPED_ORIGIN
    assert chosen["soleil"] == "https://soleil-origin.r2.test"


def _choose_with_map(health: dict[str, int | None]) -> dict[str, str | None]:
    import api.application.services.media_config as media_config

    original = media_config.settings.image_cdn_project_map
    media_config.settings.image_cdn_project_map = (
        f'{{"camellia":["{BROKEN_ORIGIN}","{MAPPED_ORIGIN}"],'
        f'"soleil":["https://soleil-origin.r2.test"]}}'
    )
    try:
        from scripts.migrate_cdn_origins import choose_origins as real_choose

        return real_choose(("camellia", "soleil"), health)
    finally:
        media_config.settings.image_cdn_project_map = original


def test_rewrite_plan_carries_the_real_image_id() -> None:
    # The guarded UPDATE matches BOTH image_id and the old URL; an empty
    # image_id silently updated zero rows (live incident 2026-08-27).
    old = f"{BROKEN_ORIGIN}/images/camellia/matbang/a.png"
    new = f"{MAPPED_ORIGIN}/images/camellia/matbang/a.png"
    rows = [{"image_id": "matbang-trang-01", "project_key": "camellia",
             "status": "published", "url_cdn": old}]
    plans = plan_rows(
        rows,
        chosen_origins={"camellia": MAPPED_ORIGIN},
        url_health={old: None, new: 200},
    )
    assert plans[0].decision == "rewrite"
    assert plans[0].image_id == "matbang-trang-01"
