"""Upload processed project videos to Cloudflare R2 (demo web playback).

Pushes the ffmpeg-processed clips from data/_processed/media/<subset> to the R2
bucket under media/video/<key>. Content type is video/mp4 so browsers stream the
objects directly; the cache header mirrors the image upload pass (immutable,
because keys are versioned by hand). Credentials come from Settings, never .env.

WHY a separate script rather than folding into upload_images_r2: videos are a
different media class with fixed, human-meaningful keys (not name-derived kinds)
and a different content type + cache policy. Keeping them isolated reads better
and avoids touching the image uploader's prefix rules.

This step only moves bytes to object storage; it does not touch Postgres. The
greeting widget lists these URLs from a static registry (media_config).
"""

from __future__ import annotations

import pathlib
import sys

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

# Make the repo root importable so `api.infrastructure.config` resolves when
# this script runs directly (ingest scripts are invoked from the repo root).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.infrastructure.config.config import settings  # noqa: E402

# Source dir holds the processed outputs (one per R2 key, named to match).
_SOURCE_DIR = _REPO_ROOT / "data" / "_processed" / "media" / "video"

# (source filename, R2 object key) — explicit so the mapping is auditable.
_UPLOADS: tuple[tuple[str, str], ...] = (
    ("dji-orbit-faststart.mp4", "media/video/dji-orbit-faststart.mp4"),
    ("brand-film-faststart.mp4", "media/video/brand-film-faststart.mp4"),
    ("brand-film-web.mp4", "media/video/brand-film-web.mp4"),
)

_CONTENT_TYPE = "video/mp4"

# Videos exceed the single-PUT comfort zone (1.1 GB drone), so drive the transfer
# through boto3's multipart uploader: it chunks large bodies, re-tries each part,
# and multithreads by default — far more reliable than one oversized read_bytes.
_TRANSFER = TransferConfig(
    multipart_threshold=64 * 1024 * 1024,
    multipart_chunksize=32 * 1024 * 1024,
    max_concurrency=8,
)

# Default SDK retries are too thin for slow WAN uploads of large parts.
_CLIENT_CONFIG = boto3.session.Config(
    signature_version="s3v4",
    retries={"max_attempts": 5, "mode": "standard"},
)


def _public_url(key: str) -> str:
    """Compose the public URL for an object key from the configured base host."""
    return f"{settings.r2_public_base}/{key}"


def main() -> int:
    """Upload each processed clip and print a per-file and aggregate summary."""
    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        region_name="auto",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=_CLIENT_CONFIG,
    )

    ok: list[str] = []
    failed: list[str] = []
    for filename, key in _UPLOADS:
        path = _SOURCE_DIR / filename
        if not path.is_file():
            failed.append(filename)
            print(f"MISSING: {path}")
            continue
        try:
            with path.open("rb") as body:
                client.upload_fileobj(
                    body,
                    settings.r2_bucket_name,
                    key,
                    Config=_TRANSFER,
                    ExtraArgs={
                        "ContentType": _CONTENT_TYPE,
                        "CacheControl": "public, max-age=31536000, immutable",
                        "Metadata": {"source": "video-ingest"},
                    },
                )
        except ClientError as exc:
            failed.append(filename)
            print(f"FAIL: {filename} -> {exc}")
            continue
        ok.append(filename)
        print(
            f"{filename} ({path.stat().st_size / 1_000_000:.1f} MB) "
            f"-> {key} -> {_public_url(key)}"
        )

    print(
        f"\nUPLOAD SUMMARY: {len(ok)} succeeded, {len(failed)} failed."
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
