"""Unit tests for ingest.register_project_images (Soleil media registration).

Covers the pure filename->caption derivation and the object-key slug shared
with the uploader, so gallery rows never drift from what R2 actually holds.
"""

from __future__ import annotations

from ingest.register_project_images import build_title_caption
from ingest.upload_images_r2 import _slugify


class TestSlugify:
    def test_spaces_and_underscores_become_dashes(self):
        assert (
            _slugify("2025.10.13_TOA D_MAT BANG CH01_STUDIO.png")
            == "2025.10.13-toa-d-mat-bang-ch01-studio.png"
        )

    def test_diacritics_are_dropped(self):
        assert _slugify("TÒA A1- TANG 54.png") == "toa-a1-tang-54.png"

    def test_extension_lowered(self):
        assert _slugify("Plan.PNG") == "plan.png"


class TestBuildTitleCaption:
    def test_tower_unit_code_and_type(self):
        title, caption = build_title_caption(
            "2025.10.13_TOA D_MAT BANG CH01_STUDIO.png"
        )
        assert title == "Tòa D — CH01 — Studio"
        assert caption == "Mặt bằng Tòa D — CH01 — Studio"

    def test_tower_and_floor_range(self):
        title, caption = build_title_caption("TOA A1_TANG 43-45.png")
        assert title == "Tòa A1 — Tầng 43-45"
        assert caption == "Mặt bằng Tòa A1 — Tầng 43-45"

    def test_diacritic_tower_and_single_floor(self):
        title, _ = build_title_caption("TÒA A1- TANG 54.png")
        assert title == "Tòa A1 — Tầng 54"

    def test_bedroom_type_mapping(self):
        title, _ = build_title_caption("2025.10.13_TOA D_MAT BANG CH12B_2BR.png")
        assert title == "Tòa D — CH12B — 2PN"

    def test_hyper_panorama_preferred_over_pano(self):
        title, _ = build_title_caption("TOA D_MAT BANG CH08_HYPER PANORAMA.png")
        assert title == "Tòa D — CH08 — Hyper Panorama"

    def test_unrecognised_name_still_yields_title(self):
        title, caption = build_title_caption("khac-lhãng.png")
        assert title
        assert caption.startswith("Mặt bằng")
