"""Ingest image metadata + vectorize captions into the local Postgres store.

Two phases:
1. Upsert one row per image into `images` from a hand-curated caption manifest
   (facts verified against the extract files, not dreamed up by vision).
2. Embed each non-empty caption with text-embedding-v4 (dims 1024 LOCK) and
   insert into `image_embeddings` (idempotent on image_id+caption_hash).

Why wrap the manifest drive in a script: PIL (size), hashlib (sha256) and the
OpenAI-compatible embed call all need a real Python process, and keeping it a
committed script lets the ingest be re-run safely (ON CONFLICT upserts).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg
import numpy as np
import openai
from PIL import Image

from ingest.config import settings


_REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = _REPO_ROOT / "ingest" / "image_captions_manifest.json"


def _sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _image_meta(source_file: str) -> tuple[int, int, str]:
    """Return (width, height, sha256) for an image under the source base dir."""
    path = _REPO_ROOT / "data" / "_processed" / "raw" / source_file
    with Image.open(path) as im:
        width, height = im.size
    content_hash = _sha256_hex(path.read_bytes())
    return width, height, content_hash


def _make_embed_client():
    """OpenAI-compatible client for text-embedding-v4 following lightrag_init.

    Why carry the /v1 suffix: the openai SDK 2.x turns a bare host into a
    plain-text response with no `.data`, so the base URL is normalized exactly
    like llm_base_url_v1 does elsewhere.
    """
    base = settings.embedding_base_url.strip()
    http_base = base if base.rstrip("/").endswith("/v1") else base.rstrip("/") + "/v1"
    return openai.AsyncOpenAI(
        api_key=settings.embedding_api_key,
        base_url=http_base,
    )


# Provider rejects a batch larger than 10 inputs, so chunk the request.
_EMBED_BATCH = 10


async def embed_captions(captions: list[str], model: str) -> list[np.ndarray]:
    """Batch-embed captions, chunked to stay under the provider's batch cap."""
    client = _make_embed_client()
    out: list[np.ndarray] = [None] * len(captions)  # type: ignore[list-item]
    for start in range(0, len(captions), _EMBED_BATCH):
        chunk = captions[start : start + _EMBED_BATCH]
        resp = await client.embeddings.create(model=model, input=chunk)
        # Sort by index for a stable result order within the chunk.
        ordered = sorted(resp.data, key=lambda d: d.index)
        for base, d in zip(range(start, start + len(chunk)), ordered):
            out[base] = np.asarray(d.embedding, dtype=np.float32)
    return out


async def ingest(conn: asyncpg.Connection) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    images = manifest["images"]
    r2_base = manifest["r2_public_base"].rstrip("/")

    captions: list[str] = []
    for img in images:
        width, height, content_hash = _image_meta(img["source_file"])
        url_cdn = f"{r2_base}/{img['r2_key']}"
        await conn.execute(
            """
            INSERT INTO images
              (image_id, kind, title, caption, alt_text, url_cdn, width, height,
               content_hash, status, source_file, linked_subject_key, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'published',$10,$11,$12)
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
              linked_subject_key = EXCLUDED.linked_subject_key,
              metadata = EXCLUDED.metadata,
              updated_at = now()
            """,
            img["image_id"],
            img["kind"],
            img["title"],
            img["caption"],
            img["alt_text"],
            url_cdn,
            width,
            height,
            content_hash,
            img["source_file"],
            img["linked_subject_key"],
            json.dumps(img["metadata"], ensure_ascii=False),
        )
        captions.append(img["caption"])

    # Embed non-empty captions only; each maps 1:1 back to its image_id.
    idx = [i for i, c in enumerate(captions) if c and c.strip()]
    bodies = [captions[i] for i in idx]
    vectors = await embed_captions(bodies, settings.embedding_model)

    for i, vec in zip(idx, vectors):
        caption = captions[i]
        image_id = images[i]["image_id"]
        caption_hash = _sha256_hex(caption.encode("utf-8"))
        await conn.execute(
            """
            INSERT INTO image_embeddings (image_id, caption_hash, embedding, model, dims)
            VALUES ($1, $2, $3::vector, $4, $5)
            ON CONFLICT (image_id, caption_hash) DO NOTHING
            """,
            image_id,
            caption_hash,
            "[" + ",".join(f"{v:.8f}" for v in vec.tolist()) + "]",
            settings.embedding_model,
            settings.embedding_dim,
        )

    print(f"upserted images={len(images)} embeddings={len(vectors)}")


async def main() -> None:
    conn = await asyncpg.connect(settings.pg_dsn)
    try:
        await ingest(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
