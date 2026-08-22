"""Regression tests for R2 CDN URL percent-encoding (QA D2).

The Camellia manifest key 'som_95%.jpg' produced a url_cdn with a raw '%'
that browsers block (ERR_BLOCKED_BY_ORB). These tests lock the builder
contract: generated URLs never contain a '%' that is not a valid escape,
while clean keys pass through byte-for-byte.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from ingest.images_ingest import build_cdn_url

# A '%' that is not the start of a percent-encoded byte is a raw, illegal '%'.
_RAW_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")

_BASE = "https://pub-example.r2.dev"


def test_percent_in_object_key_is_encoded() -> None:
    url = build_cdn_url(
        _BASE, "images/thanh-toan/phuong_thuc_phuong_an_thanh_toan_som_95%.jpg"
    )
    assert url.endswith("som_95%25.jpg")
    assert _RAW_PERCENT.search(url) is None
    # The URL still addresses the same object: decoding restores the raw key.
    assert unquote(url).endswith("phuong_thuc_phuong_an_thanh_toan_som_95%.jpg")


def test_spaces_are_encoded_and_slashes_preserved() -> None:
    url = build_cdn_url(_BASE + "/", "images/matbang/my plan 2.png")
    assert url == f"{_BASE}/images/matbang/my%20plan%202.png"
    assert _RAW_PERCENT.search(url) is None


def test_clean_key_passes_through_unchanged() -> None:
    key = "images/matbang/matbang-02.png"
    assert build_cdn_url(_BASE, key) == f"{_BASE}/{key}"


def test_every_manifest_r2_key_builds_a_legal_url() -> None:
    import json
    from pathlib import Path

    manifest = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "ingest"
            / "image_captions_manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["images"], "manifest must not be empty"
    for img in manifest["images"]:
        url = build_cdn_url(manifest["r2_public_base"], img["r2_key"])
        assert _RAW_PERCENT.search(url) is None, img["image_id"]
        assert unquote(url) == f"{manifest['r2_public_base'].rstrip('/')}/{img['r2_key']}"
