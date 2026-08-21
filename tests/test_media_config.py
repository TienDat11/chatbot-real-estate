"""Unit tests for the static project video registry (greeting widget).

Verifies the display contract the frontend consumes (title/kind/urls/dimensions)
and that URLs are derived from ``settings.r2_public_base`` rather than a
hardcoded host. Reloading the module under a patched setting proves the base is
read from config, not baked in.
"""

from __future__ import annotations

import importlib

import api.application.services.media_config as media_config
from api.infrastructure.config.config import Settings, settings

EXPECTED_KEYS = {
    "title",
    "kind",
    "url_cdn",
    "poster_url",
    "width",
    "height",
    "duration",
    "bytes_mb",
}


def test_list_project_videos_returns_three_videos():
    assert len(media_config.list_project_videos()) == 3


def test_videos_match_contract_shape():
    for video in media_config.list_project_videos():
        assert set(video) == EXPECTED_KEYS


def test_video_kinds_are_brand_or_drone():
    for video in media_config.list_project_videos():
        assert video["kind"] in {"brand", "drone"}


def test_titles_have_no_em_or_en_dash():
    # Em/en-dash is a display-only hard rule in this project's UI surface.
    for video in media_config.list_project_videos():
        assert "\u2014" not in video["title"]
        assert "\u2013" not in video["title"]


def test_url_cdn_built_from_settings_r2_public_base():
    base = settings.r2_public_base
    for video in media_config.list_project_videos():
        assert video["url_cdn"].startswith(base + "/media/video/")
        assert video["poster_url"].startswith(base + "/")


def test_url_cdn_not_hardcoded_when_settings_change():
    # Swap the public base and reload: a hardcoded host would survive the swap
    # and fail the assertions below, proving the registry reads config.
    original = Settings.r2_public_base
    try:
        Settings.r2_public_base = property(lambda self: "https://cdn.example.test")
        importlib.reload(media_config)
        videos = media_config.list_project_videos()
        assert len(videos) == 3
        for video in videos:
            assert video["url_cdn"].startswith("https://cdn.example.test/media/video/")
            assert video["poster_url"].startswith("https://cdn.example.test/")
    finally:
        Settings.r2_public_base = original
        importlib.reload(media_config)


def test_order_leads_with_light_brand_film():
    # The lightest web-appropriate clip must lead so the widget never defaults
    # to the heavy original/drone download.
    videos = media_config.list_project_videos()
    assert videos[0]["kind"] == "brand"
    assert any(video["kind"] == "drone" for video in videos)
