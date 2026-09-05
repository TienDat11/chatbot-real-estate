"""Unit tests for the per-project video registry (greeting widget).

The registry lives in ``project_config.media``; the module resolves DB object
keys to public URLs through the project CDN map at call time. Contract tests
stub ``_media_from_registry`` with a canonical Camellia bundle (mirroring what
the real read returns after ``_resolve_media_row``) so the suite is hermetic —
no test depends on a live DB. Policy tests cover the fail-closed URL seam and
the no-cross-project-borrow rule (missing/empty registry row -> empty list).
"""

from __future__ import annotations

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

# DB-shaped Camellia bundle (object keys, as stored in project_config.media);
# the stub resolves it through the same _resolve_media_row path production uses.
_CAMELLIA_ENTRIES = (
    {"title": "The Camellia - Brand Film (Web)", "kind": "brand", "object_key": "media/video/brand-film-web.mp4", "poster_key": "images/matbang/matbang-02.png", "width": 1920, "height": 1080, "duration": None, "bytes_mb": None},
    {"title": "The Camellia - Brand Film (Original)", "kind": "brand", "object_key": "media/video/brand-film-faststart.mp4", "poster_key": "images/matbang/matbang-02.png", "width": 1920, "height": 1080, "duration": None, "bytes_mb": None},
    {"title": "The Camellia - Drone Overview (DJI)", "kind": "drone", "object_key": "media/video/dji-orbit-faststart.mp4", "poster_key": "images/matbang/matbang-02.png", "width": None, "height": None, "duration": None, "bytes_mb": None},
)


def _stub_camellia_registry(project_key):
    """Mirror the production read: entries -> display rows via _resolve_media_row."""
    return [media_config._resolve_media_row(entry, project_key) for entry in _CAMELLIA_ENTRIES]


def test_list_project_videos_returns_three_videos(monkeypatch):
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    assert len(media_config.list_project_videos()) == 3


def test_videos_match_contract_shape(monkeypatch):
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    for video in media_config.list_project_videos():
        assert set(video) == EXPECTED_KEYS


def test_video_kinds_are_brand_or_drone(monkeypatch):
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    for video in media_config.list_project_videos():
        assert video["kind"] in {"brand", "drone"}


def test_titles_have_no_em_or_en_dash(monkeypatch):
    # Em/en-dash is a display-only hard rule in this project's UI surface.
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    for video in media_config.list_project_videos():
        assert "\u2014" not in video["title"]
        assert "\u2013" not in video["title"]


def test_url_cdn_built_from_settings_r2_public_base(monkeypatch):
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    base = settings.image_cdn_base("camellia")
    for video in media_config.list_project_videos():
        assert video["url_cdn"].startswith(base + "/media/video/")
        assert video["poster_url"].startswith(base + "/")


def test_url_cdn_not_hardcoded_when_settings_change(monkeypatch):
    # One source of truth per project: a mapped origin wins over the global
    # R2_PUBLIC_URL, and the resolved URLs follow the map (a hardcoded host
    # would keep serving the old base and fail the assertion below).
    monkeypatch.setattr(
        media_config.settings, "image_cdn_project_map", '{"camellia":"https://cdn.example.test"}'
    )
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    videos = media_config.list_project_videos("camellia")
    assert len(videos) == 3
    for video in videos:
        assert video["url_cdn"].startswith("https://cdn.example.test/media/video/")
        assert video["poster_url"].startswith("https://cdn.example.test/")


def test_invalid_media_origin_is_rejected(monkeypatch):
    monkeypatch.setattr(media_config, "_public_base", "https://evil.example")
    assert media_config._valid_media_url("https://evil.example/video.mp4", "camellia") is None
    assert media_config._valid_media_url(
        settings.image_cdn_base("camellia") + "/images/camellia/matbang/video.mp4",
        "camellia",
        "gallery",
    )


def test_credential_bearing_urls_reject_empty_username_and_password(monkeypatch):
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":"https://cdn.example.test"}',
    )
    assert media_config.valid_media_url("https://@cdn.example.test/image.png", "camellia") is None
    assert (
        media_config.valid_media_url("https://:secret@cdn.example.test/image.png", "camellia")
        is None
    )
    assert (
        media_config.valid_media_url("https://user@cdn.example.test/image.png", "camellia")
        is None
    )
    assert (
        media_config.valid_media_url(
            "https://user:secret@cdn.example.test/image.png", "camellia"
        )
        is None
    )


def test_unconfigured_project_origin_is_rejected_even_when_other_project_is_configured(monkeypatch):
    monkeypatch.setattr(media_config.settings, "image_cdn_project_map", '{"camellia":"https://cdn.example.test"}')
    assert media_config.valid_media_url("https://cdn.example.test/image.png", "soleil") is None


def test_unconfigured_map_uses_dev_r2_fixture_origin_but_production_fails_closed(monkeypatch):
    monkeypatch.setattr(media_config.settings, "image_cdn_project_map", "{}")
    monkeypatch.setattr(media_config.settings, "r2_public_url", "https://fixture.example.test")
    monkeypatch.setattr(media_config.settings, "app_env", "test")
    assert media_config.valid_media_url("https://fixture.example.test/image.png", "camellia")

    monkeypatch.setattr(media_config.settings, "app_env", "production")
    assert (
        media_config.valid_media_url("https://fixture.example.test/image.png", "camellia")
        is None
    )


def test_registry_media_filters_invalid_origin(monkeypatch):
    monkeypatch.setattr(
        media_config,
        "_media_from_registry",
        lambda project_key: [{"url_cdn": "https://evil.example/video.mp4", "poster_url": None}],
    )
    # The direct helper is the security boundary for rows converted from registry.
    assert media_config._valid_media_url("https://evil.example/video.mp4", "camellia") is None


def test_explicit_public_url_is_active_origin_and_blocks_derived_host(monkeypatch):
    monkeypatch.setattr(media_config.settings, "r2_public_url", "https://images.example.test")
    monkeypatch.setattr(media_config.settings, "r2_account_id", "wrong-account")
    monkeypatch.setattr(
        media_config.settings, "image_cdn_project_map", '{"camellia":"https://images.example.test"}'
    )
    assert media_config.valid_media_url("https://images.example.test/a.png", "camellia")
    assert (
        media_config.valid_media_url("https://pub-wrong-account.r2.dev/a.png", "camellia") is None
    )


def test_project_map_allows_project_origin_but_unknown_origin_fails(monkeypatch):
    monkeypatch.setattr(media_config.settings, "r2_public_url", "")
    monkeypatch.setattr(media_config.settings, "r2_account_id", "")
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"soleil":["https://soleil.example.test"]}',
    )
    assert media_config.valid_media_url("https://soleil.example.test/gallery.png", "soleil")
    assert media_config.valid_media_url("https://unknown.example/gallery.png", "soleil") is None
    assert (
        media_config.valid_media_url("https://soleil.example.test/gallery.png", "camellia") is None
    )


def test_structured_project_map_is_not_stringified(monkeypatch):
    cfg = Settings(
        _env_file=None,
        r2_account_id="account",
        image_cdn_project_map=(
            '{"camellia":{"origins":["https://current.example.test"],'
            '"path_prefixes":["images/camellia/"]}}'
        ),
    )
    assert cfg.image_cdn_base("camellia") == "https://current.example.test"
    assert cfg.image_cdn_path_prefixes("camellia") == ("images/camellia/",)
    assert cfg.image_cdn_base("soleil") == ""


def test_explicit_project_map_does_not_fallback_to_account_origin(monkeypatch):
    monkeypatch.setattr(media_config.settings, "r2_account_id", "wrong-account")
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":"https://camellia.example.test"}',
    )
    assert media_config.settings.image_cdn_base("soleil") == ""
    assert media_config.allowed_media_origins("soleil") == set()


def test_same_origin_wrong_project_namespace_is_rejected(monkeypatch):
    monkeypatch.setattr(media_config.settings, "r2_public_url", "")
    monkeypatch.setattr(media_config.settings, "r2_account_id", "")
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":"https://shared.example.test","soleil":"https://shared.example.test"}',
    )
    assert (
        media_config.valid_media_url(
            "https://shared.example.test/images/camellia/matbang/a.png", "soleil", "gallery"
        )
        is None
    )
    assert media_config.valid_media_url(
        "https://shared.example.test/images/soleil/matbang/a.png", "soleil", "gallery"
    )


def test_unknown_project_and_registry_failure_fail_closed(monkeypatch):
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":"https://camellia.example.test","soleil":"https://soleil.example.test"}',
    )
    assert (
        media_config.valid_media_url(
            "https://camellia.example.test/images/camellia/a.png", "unknown", "gallery"
        )
        is None
    )
    monkeypatch.setattr(media_config, "_media_from_registry", lambda _: None)
    assert media_config.list_project_videos("soleil") == []


def test_both_project_origins_are_valid_only_in_their_namespace(monkeypatch):
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":"https://camellia.example.test","soleil":"https://soleil.example.test"}',
    )
    assert media_config.valid_media_url(
        "https://camellia.example.test/images/camellia/a.png", "camellia", "gallery"
    )
    assert media_config.valid_media_url(
        "https://soleil.example.test/images/soleil/a.png", "soleil", "gallery"
    )


def test_banggia_is_allowed_for_configured_projects_and_origin_only(monkeypatch):
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":{"origins":["https://shared.example.test"],"path_prefixes":["images/banggia/"]},'
        '"soleil":{"origins":["https://shared.example.test"],"path_prefixes":["images/banggia/"]}}',
    )
    assert media_config.valid_media_url(
        "https://shared.example.test/images/banggia/camellia.png", "camellia", "gallery"
    )
    assert media_config.valid_media_url(
        "https://shared.example.test/images/banggia/soleil.png", "soleil", "gallery"
    )
    assert media_config.valid_media_url(
        "https://unconfigured.example.test/images/banggia/camellia.png", "camellia", "gallery"
    ) is None
    assert media_config.valid_media_url(
        "https://shared.example.test/images/banggia/camellia.png", "unknown", "gallery"
    ) is None


def test_banggia_cross_project_origin_and_hostile_paths_are_denied(monkeypatch):
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":{"origins":["https://camellia.example.test"],"path_prefixes":["images/banggia/"]},'
        '"soleil":{"origins":["https://soleil.example.test"],"path_prefixes":["images/banggia/"]}}',
    )
    assert media_config.valid_media_url(
        "https://soleil.example.test/images/banggia/price.png", "camellia", "gallery"
    ) is None
    assert media_config.valid_media_url(
        "https://camellia.example.test/images/../secrets.png", "camellia", "gallery"
    ) is None
    assert media_config.valid_media_url(
        "https://camellia.example.test/images/banggia/../../secrets.png", "camellia", "gallery"
    ) is None


def test_video_poster_and_gallery_paths_are_project_scoped(monkeypatch):
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":{"origins":["https://camellia.example.test"],"path_prefixes":["media/video/","images/matbang/","images/camellia/"]},"soleil":{"origins":["https://soleil.example.test"],"path_prefixes":["images/soleil/"]}}',
    )
    row = media_config._resolve_media_row(
        {
            "title": "clip",
            "kind": "brand",
            "object_key": "media/video/clip.mp4",
            "poster_key": "images/matbang/poster.png",
        },
        "camellia",
    )
    assert row["url_cdn"] == "https://camellia.example.test/media/video/clip.mp4"
    assert row["poster_url"] == "https://camellia.example.test/images/matbang/poster.png"
    assert media_config.valid_media_url(
        "https://soleil.example.test/images/soleil/gallery.png", "soleil", "gallery"
    )
    assert (
        media_config.valid_media_url(
            "https://soleil.example.test/media/video/clip.mp4", "soleil", "video"
        )
        is None
    )


def test_zero_resolve_warning_is_redacted(caplog):
    caplog.set_level("WARNING", logger="api.media_config")
    media_config._warn_zero_resolve(
        "camellia",
        [{"object_key": "bad.mp4"}, {"object_key": "also-bad.mp4"}],
        [{"url_cdn": None}, {"url_cdn": None}],
    )

    assert "media entries resolved to zero valid urls" in caplog.text
    assert "project_key=camellia" in caplog.text
    assert "entry_count=2" in caplog.text
    assert "invalid_url_count=2" in caplog.text
    assert "bad.mp4" not in caplog.text


def test_zero_resolve_warning_is_silent_when_all_urls_resolve(caplog):
    caplog.set_level("WARNING", logger="api.media_config")
    media_config._warn_zero_resolve(
        "camellia",
        [{"object_key": "ok.mp4"}],
        [{"url_cdn": "https://cdn.example/ok.mp4"}],
    )

    assert "media entries resolved to zero valid urls" not in caplog.text


def test_order_leads_with_light_brand_film(monkeypatch):
    # The lightest web-appropriate clip must lead so the widget never defaults
    # to the heavy original/drone download.
    monkeypatch.setattr(media_config, "_media_from_registry", _stub_camellia_registry)
    videos = media_config.list_project_videos("camellia")
    assert videos[0]["kind"] == "brand"
    assert any(video["kind"] == "drone" for video in videos)


def test_project_map_entry_wins_over_global_r2_public_url(monkeypatch):
    # FR-13 env reconciliation: ONE source of truth per project. A mapped
    # project resolves ONLY to its map origin even when R2_PUBLIC_URL differs;
    # the global URL stays relevant solely for deployments with no map entry.
    monkeypatch.setattr(media_config.settings, "r2_public_url", "https://global.example.test")
    monkeypatch.setattr(
        media_config.settings,
        "image_cdn_project_map",
        '{"camellia":"https://mapped.example.test","soleil":"https://soleil.example.test"}',
    )
    assert media_config.allowed_media_origins("camellia") == {"https://mapped.example.test"}
    assert settings.image_cdn_base("camellia") == "https://mapped.example.test"
    assert media_config.allowed_media_origins("soleil") == {"https://soleil.example.test"}
    assert "global.example.test" not in str(media_config.allowed_media_origins("camellia"))
