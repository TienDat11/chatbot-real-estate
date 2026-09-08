"""Upload and register one project's image corpus in a namespaced path."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import unicodedata

import boto3
import psycopg2
from PIL import Image

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.infrastructure.config.config import settings  # noqa: E402
from ingest.upload_images_r2 import CONTENT_TYPES, slugify  # noqa: E402


def _caption(filename: str) -> str:
    """Build a stable searchable caption from a source filename."""
    folded = unicodedata.normalize("NFKD", filename)
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return f"Mặt bằng dự án {plain.rsplit('.', 1)[0].replace('_', ' ')}"


def _rows(project_key: str, src_dir: pathlib.Path) -> list[dict[str, object]]:
    """Return image rows with project-specific storage keys and URLs."""
    base = settings.image_cdn_base(project_key)
    rows: list[dict[str, object]] = []
    for path in sorted(src_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in CONTENT_TYPES:
            continue
        key = f"images/{project_key}/matbang/{slugify(path.name)}"
        with Image.open(path) as image:
            width, height = image.size
        rows.append(
            {
                "image_id": f"{project_key}-{slugify(path.name).rsplit('.', 1)[0]}",
                "title": path.stem.replace("_", " "),
                "caption": _caption(path.name),
                "alt_text": _caption(path.name),
                "url_cdn": f"{base}/{key}",
                "width": width,
                "height": height,
                "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_file": str(path.resolve().relative_to(_REPO_ROOT)).replace("\\", "/"),
                "path": path,
                "key": key,
            }
        )
    return rows


_UPSERT = """
INSERT INTO images (image_id, kind, title, caption, alt_text, url_cdn, width, height,
 content_hash, status, source_file, metadata, project_key)
VALUES (%(image_id)s, 'matbang', %(title)s, %(caption)s, %(alt_text)s, %(url_cdn)s,
 %(width)s, %(height)s, %(content_hash)s, 'published', %(source_file)s,
 %(metadata)s::jsonb, %(project_key)s)
ON CONFLICT (image_id) DO UPDATE SET title=EXCLUDED.title, caption=EXCLUDED.caption,
 alt_text=EXCLUDED.alt_text, url_cdn=EXCLUDED.url_cdn, width=EXCLUDED.width,
 height=EXCLUDED.height, content_hash=EXCLUDED.content_hash, status='published',
 source_file=EXCLUDED.source_file, metadata=EXCLUDED.metadata,
 project_key=EXCLUDED.project_key, updated_at=now()
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--src-dir", type=pathlib.Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    rows = _rows(args.project_key, args.src_dir)
    if not rows:
        print("No images found.")
        return 1
    for row in rows:
        print(f"{row['image_id']} -> {row['url_cdn']}")
    if args.dry_run:
        return 0
    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        region_name="auto",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
    )
    with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            for row in rows:
                path = row["path"]
                client.upload_file(
                    str(path),
                    settings.r2_bucket_name,
                    row["key"],
                    ExtraArgs={"ContentType": CONTENT_TYPES[path.suffix.lower()]},
                )
                payload = {"original_filename": path.name}
                row.update(project_key=args.project_key, metadata=json.dumps(payload))
                cur.execute(_UPSERT, row)
    print(f"REGISTERED: {len(rows)} rows for project '{args.project_key}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
