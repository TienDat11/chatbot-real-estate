"""Upload a project image corpus to Cloudflare R2 (Story: image hosting).

Default pass keeps the original Camellia behaviour: scans data/_processed/raw
for the four image groups the RAG pipeline references (matbang, bang gia,
to roi, phuong thuc thanh toan) and pushes each to the R2 bucket under
images/<kind>/<original-filename>. Credentials come from
api.infrastructure.config.config, never hardcoded.

Other projects (e.g. Soleil) differ in source folder and naming, so the pass
is parameterised: --src-dir points at the corpus, --kind forces one bucket
kind when prefix rules do not apply, and object keys are slugified because
source filenames may contain spaces (S3-legal keys, URL-hostile unencoded).

Deliberately excluded (Camellia defaults only): phaplv-*.png (legal) and
qna-*.png (QnA) — a hard prefix filter keeps them out even if the source
folder later gains more files.

This step only moves bytes to object storage; it does not touch Postgres, the
vector store, or the images table (a later step owns that).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import unicodedata

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


def _slugify(filename: str) -> str:
    """Collapse a source filename into a URL-safe ASCII slug for the object key.

    Spaces and underscores become dashes so the public URL never needs
    percent-encoding, and NFKD folding drops tone marks without deleting the
    base letter ("TÒA" -> "toa", never "ta"); the original name survives in
    object metadata.
    """
    stem = filename.rsplit(".", 1)[0].lower().replace("_", " ")
    folded = unicodedata.normalize("NFKD", stem)
    ascii_stem = "".join(c for c in folded if not unicodedata.combining(c))
    slug = re.sub(r"\s+", "-", ascii_stem)
    slug = re.sub(r"[^a-z0-9.\-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return f"{slug}.{filename.rsplit('.', 1)[1].lower()}"


def _kind_for(filename: str) -> str | None:
    """Return the R2 object kind for a filename, or None if it is not in scope."""
    for prefix, kind in _KIND_RULES:
        if filename.startswith(prefix):
            return kind
    return None


def _public_url(key: str) -> str:
    """Compose the public URL for an object key from the configured base host."""
    return f"{settings.r2_public_base}/{key}"


def _collect(
    src_dir: pathlib.Path, forced_kind: str | None
) -> tuple[list[tuple[pathlib.Path, str]], list[str]]:
    """Return (in-scope files with their kind, excluded filenames found)."""
    in_scope: list[tuple[pathlib.Path, str]] = []
    excluded: list[str] = []
    for path in sorted(src_dir.iterdir()):
        if not path.is_file():
            continue
        # A forced-kind corpus folder may mix documents with images; only
        # image extensions are in scope for this uploader.
        if not forced_kind or path.suffix.lower() in _CONTENT_TYPES:
            name = path.name
            if not forced_kind and any(
                name.startswith(prefix) for prefix in _EXCLUDED_PREFIXES
            ):
                excluded.append(name)
                continue
            kind = forced_kind or _kind_for(name)
            if kind is not None:
                in_scope.append((path, kind))
    return in_scope, excluded


def _upload(client, path: pathlib.Path, kind: str, slugify: bool) -> str:
    """Upload one image and return its object key."""
    name = _slugify(path.name) if slugify else path.name
    key = f"images/{kind}/{name}"
    content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    # S3 object metadata must be ASCII; Vietnamese source names get folded so
    # the traceability hint survives without breaking the put.
    ascii_name = (
        unicodedata.normalize("NFKD", path.name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    client.put_object(
        Bucket=settings.r2_bucket_name,
        Key=key,
        Body=path.read_bytes(),
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
        Metadata={"source": "rag-ingest", "original-filename": ascii_name},
    )
    return key


def main(argv: list[str] | None = None) -> int:
    """Run the upload pass and print a per-file and aggregate summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src-dir",
        type=pathlib.Path,
        default=_RAW_DIR,
        help="Folder holding the corpus to upload (default: Camellia raw dir).",
    )
    parser.add_argument(
        "--kind",
        dest="forced_kind",
        default=None,
        help="Force one R2 kind for every file when prefix rules do not apply "
        "(e.g. 'matbang' for a floor-plan-only corpus).",
    )
    parser.add_argument(
        "--slugify",
        action="store_true",
        help="Slugify object-key filenames (needed for names with spaces).",
    )
    args = parser.parse_args(argv)

    if not args.src_dir.is_dir():
        print(f"Source folder not found: {args.src_dir}")
        return 1

    in_scope, excluded = _collect(args.src_dir, args.forced_kind)

    for name in excluded:
        print(f"SKIP (excluded group): {name}")

    if not in_scope:
        print(f"No in-scope images found under {args.src_dir}.")
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
            key = _upload(client, path, kind, args.slugify)
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
