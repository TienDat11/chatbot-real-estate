"""Upload the Camellia image corpus to Cloudflare R2 (Story: image hosting).

Scans data/_processed/raw for the four image groups that the RAG pipeline
references (matbang, bang gia, to roi, phuong thuc thanh toan) and pushes each
to the R2 bucket under images/<kind>/<original-filename>. Credentials come from
Settings (api.infrastructure.config.config), never hardcoded.

Deliberately excluded: phaplv-*.png (legal) and qna-*.png (QnA) — those groups
are not part of this upload pass, and a hard prefix filter keeps them out even
if the source folder later gains more files.

This step only moves bytes to object storage; it does not touch Postgres, the
vector store, or the images table (a later step owns that).
"""

from __future__ import annotations

import pathlib
import sys
from typing import Iterable

import boto3
from botocore.exceptions import ClientError

# Make the repo root importable so `api.infrastructure.config` resolves when
# this script runs directly (ingest scripts are invoked from the repo root).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.infrastructure.config.config import settings  # noqa: E402

_RAW_DIR = _REPO_ROOT / "data" / "_processed" / "raw"

# Prefix -> R2 object kind. Order matters: the more specific prefixes are listed
# first so a name never falls through to a wrong bucket.
_KIND_RULES: tuple[tuple[str, str], ...] = (
    ("matbang-", "matbang"),
    ("gia-", "banggia"),
    ("toroi-", "toroi"),
    ("phuong_thuc", "thanh-toan"),
    ("phuong_an", "thanh-toan"),
)

# Hard-excluded groups; warn loudly if any appear in the source folder.
_EXCLUDED_PREFIXES: tuple[str, ...] = ("phaplv-", "qna-")

_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _kind_for(filename: str) -> str | None:
    """Return the R2 object kind for a filename, or None if it is not in scope."""
    for prefix, kind in _KIND_RULES:
        if filename.startswith(prefix):
            return kind
    return None


def _public_url(key: str) -> str:
    """Compose the public URL for an object key from the configured base host."""
    return f"{settings.r2_public_base}/{key}"


def _collect() -> tuple[list[tuple[pathlib.Path, str]], list[str]]:
    """Return (in-scope files with their kind, excluded filenames found)."""
    in_scope: list[tuple[pathlib.Path, str]] = []
    excluded: list[str] = []
    for path in sorted(_RAW_DIR.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if any(name.startswith(prefix) for prefix in _EXCLUDED_PREFIXES):
            excluded.append(name)
            continue
        kind = _kind_for(name)
        if kind is not None:
            in_scope.append((path, kind))
    return in_scope, excluded


def _upload(client, path: pathlib.Path, kind: str) -> str:
    """Upload one image and return its object key."""
    key = f"images/{kind}/{path.name}"
    content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=path.read_bytes(),
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
        Metadata={"source": "rag-ingest"},
    )
    return key


def main() -> int:
    """Run the upload pass and print a per-file and aggregate summary."""
    in_scope, excluded = _collect()

    for name in excluded:
        print(f"SKIP (excluded group): {name}")

    if not in_scope:
        print("No in-scope images found under data/_processed/raw.")
        return 1

    client = boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        region_name="auto",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=boto3.session.Config(signature_version="s3v4"),
    )

    ok: list[str] = []
    failed: list[str] = []
    for path, kind in in_scope:
        try:
            key = _upload(client, path, kind)
        except ClientError as exc:
            failed.append(path.name)
            print(f"FAIL: {path.name} -> {exc}")
            continue
        ok.append(path.name)
        print(f"{path.name} -> {key} -> {_public_url(key)}")

    print(f"\nUPLOAD SUMMARY: {len(ok)} succeeded, {len(failed)} failed, "
          f"{len(excluded)} excluded.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
