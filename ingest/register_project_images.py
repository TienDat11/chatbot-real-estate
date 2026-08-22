"""Register uploaded project images as gallery rows in the images table.

Companion to upload_images_r2: after bytes land in R2 under
images/<kind>/<slug>, this pass creates/updates one published row per file so
the project-scoped gallery (filter_images_by_project) can serve them. Captions
are derived deterministically from the original filename — good enough for
floor-plan corpora whose names encode tower/floor/unit-type — and a later
OCR/enrichment pass may refine them.

Deliberately no embedding here: the gallery path filters by project_key and
status only; text-search vectors are owned by images_ingest and depend on
external embedding quota.

Re-running is safe: image_id is derived from the slug, and the upsert
refreshes every mutable column.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import unicodedata

import psycopg2
from PIL import Image

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.infrastructure.config.config import settings  # noqa: E402
from ingest.upload_images_r2 import _CONTENT_TYPES, _slugify  # noqa: E402

# Vietnamese display labels for the unit-type tokens found in floor-plan names.
_UNIT_TYPE_LABELS: tuple[tuple[str, str], ...] = (
    ("HYPER PANORAMA", "Hyper Panorama"),
    ("HYPER PANO", "Hyper Panorama"),
    ("PANORAMA", "Panorama"),
    ("PANO", "Panorama"),
    ("S-HYPER", "S-Hyper"),
    ("3BR", "3PN"),
    ("2BR", "2PN"),
    ("1BR", "1PN"),
    ("STUDIO", "Studio"),
)


def _fold_ascii(text: str) -> str:
    """Lowercase + strip diacritics so regexes never depend on tone marks.

    Underscores become spaces first: they are word characters for \b, so
    "_TOA D_MAT BANG" would otherwise never match token patterns.
    """
    folded = unicodedata.normalize("NFKD", text.replace("_", " "))
    return "".join(c for c in folded if not unicodedata.combining(c)).upper()


def build_title_caption(filename: str) -> tuple[str, str]:
    """Derive (title, caption) from a Soleil-style floor-plan filename.

    Recognised tokens: TOA <code>, TANG <n[-m]>, MAT BANG, CH<nn>, and a
    trailing unit type. Anything unrecognised still yields a usable title
    from the slug so no file is left unnamed.
    """
    folded = _fold_ascii(filename)
    parts: list[str] = []

    tower = re.search(r"\bTOA\s+([A-Z0-9]+)", folded)
    if tower:
        parts.append(f"Tòa {tower.group(1)}")
    floor = re.search(r"\bTANG\s+(\d+(?:-\d+)?)", folded)
    if floor:
        parts.append(f"Tầng {floor.group(1)}")

    type_label = ""
    for token, label in _UNIT_TYPE_LABELS:
        if re.search(rf"\b{re.escape(token)}\b", folded):
            type_label = label
            break
    unit_code = re.search(r"\b(CH\d+[A-Z]?)\b", folded)
    if unit_code:
        parts.append(unit_code.group(1))
    if type_label:
        parts.append(type_label)

    if not parts:
        stem = _slugify(filename).rsplit(".", 1)[0].replace("-", " ")
        parts.append(stem.title() or "Tổng thể")

    title = " — ".join(parts)
    return title, f"Mặt bằng {title}"


def _image_rows(project: str, src_dir: pathlib.Path, kind: str, r2_base: str):
    """Yield one dict of column values per in-scope image file."""
    for path in sorted(src_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _CONTENT_TYPES:
            continue
        slug = _slugify(path.name)
        stem = slug.rsplit(".", 1)[0]
        title, caption = build_title_caption(path.name)
        with Image.open(path) as img:
            width, height = img.size
        yield {
            "image_id": f"{project}-{stem}",
            "kind": kind,
            "title": title,
            "caption": caption,
            "alt_text": caption,
            "url_cdn": f"{r2_base}/images/{kind}/{slug}",
            "width": width,
            "height": height,
            "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_file": str(path.resolve().relative_to(_REPO_ROOT)).replace("\\", "/"),
            "metadata": json.dumps({"original_filename": path.name}),
            "project_key": project,
        }


_UPSERT = """
INSERT INTO images
  (image_id, kind, title, caption, alt_text, url_cdn, width, height,
   content_hash, status, source_file, linked_subject_key, metadata, project_key)
VALUES (%(image_id)s, %(kind)s, %(title)s, %(caption)s, %(alt_text)s, %(url_cdn)s,
        %(width)s, %(height)s, %(content_hash)s, 'published', %(source_file)s,
        NULL, %(metadata)s::jsonb, %(project_key)s)
ON CONFLICT (image_id) DO UPDATE SET
  kind = EXCLUDED.kind,
  title = EXCLUDED.title,
  caption = EXCLUDED.caption,
  alt_text = EXCLUDED.alt_text,
  url_cdn = EXCLUDED.url_cdn,
  width = EXCLUDED.width,
  height = EXCLUDED.height,
  content_hash = EXCLUDED.content_hash,
  status = EXCLUDED.status,
  source_file = EXCLUDED.source_file,
  metadata = EXCLUDED.metadata::jsonb,
  project_key = EXCLUDED.project_key,
  updated_at = now()
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project_key to scope rows to")
    parser.add_argument("--src-dir", type=pathlib.Path, required=True)
    parser.add_argument("--kind", default="matbang")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.src_dir.is_dir():
        print(f"Source folder not found: {args.src_dir}")
        return 1

    rows = list(_image_rows(args.project, args.src_dir, args.kind, settings.r2_public_base))
    if not rows:
        print(f"No images under {args.src_dir}.")
        return 1

    for row in rows[:3] + (["..."] if len(rows) > 3 else []):
        print(row if isinstance(row, str) else f"{row['image_id']} -> {row['url_cdn']}")
    print(f"rows: {len(rows)}")

    if args.dry_run:
        return 0

    with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(_UPSERT, row)
    print(f"REGISTERED: {len(rows)} rows for project '{args.project}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
