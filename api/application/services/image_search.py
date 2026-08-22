"""Image search — semantic retrieval of illustrative images to accompany answers.

A vector + rel scan over `image_embeddings` (dim 1024, text-embedding-v4) joined
to `images`. This is a best-effort enrichment of the answer payload: any failure
(embedding call or DB) degrades to an empty list so the pipeline never crashes.

Unit-precise re-ranking: when the query names a concrete unit code (e.g. "căn
CH-03"), the vector dist over the caption alone is a poor proxy for the exact
floor-plan the user asked for. We therefore re-order any exact `unit:` match to
the head (querying the index directly when the vector pass missed it) and keep
same-type units behind it for visual comparison. Plain semantic queries without
a unit code keep the raw score order. Every returned item carries a `match`
label and a short human `reason` so the UI can explain why an image is shown.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from api.application.services.sql_leg import with_rls_identity
from api.infrastructure.config.config import settings

logger = logging.getLogger("api.image_search")

# A concrete unit code looks like CH-03, CH-03A or CH-9. Capturing this lets the
# re-ranker pivot from fuzzy caption similarity to an exact floor-plan match,
# which is the only ordering a user who names a specific unit actually expects.
_UNIT_CODE_RE = re.compile(r"\bCH-\d{1,2}[A-Z]?\b", re.IGNORECASE)

# Only non-legal/non-QA kinds illustrate answers; policy images are shown, not
# generated here, so phaply/qna are excluded to avoid surfacing legal artifacts.
IMAGE_QUERY = """
SELECT i.image_id, i.kind, i.title, i.caption, i.alt_text, i.url_cdn,
       i.width, i.height, i.linked_subject_key, i.metadata,
       1 - (e.embedding <=> $1::vector) AS score
FROM image_embeddings e
JOIN images i ON i.image_id = e.image_id
WHERE i.status = 'published' AND i.kind NOT IN ('phaply', 'qna')
ORDER BY e.embedding <=> $1::vector
LIMIT $2
"""

# Direct index lookup by stable unit key; used only to rescue the exact unit when
# the vector pass failed to surface it. No embedding needed, so the score is
# assigned a ceiling constant (exact intent) by the caller.
QUERY_BY_UNIT = """
SELECT i.image_id, i.kind, i.title, i.caption, i.alt_text, i.url_cdn,
       i.width, i.height, i.linked_subject_key, i.metadata
FROM images i
WHERE i.status = 'published'
  AND i.kind NOT IN ('phaply', 'qna')
  AND (i.linked_subject_key = $1 OR i.metadata->>'unit' = $2)
LIMIT 1
"""

# Representative project imagery for the first-open greeting: published images of
# a given kind with no unit link, ordered by a display-type preference list so the
# welcome always leads with the most visual asset (cover/render before amenity).
PROJECT_IMAGES_QUERY = """
SELECT i.image_id, i.kind, i.title, i.caption, i.alt_text, i.url_cdn,
       i.width, i.height, i.linked_subject_key, i.metadata,
       i.metadata->>'type' AS display_type
FROM images i
WHERE i.status = 'published'
  AND i.kind = $1
  AND i.linked_subject_key IS NULL
  AND i.metadata->>'type' = ANY (string_to_array($2, ','))
ORDER BY array_position(string_to_array($2, ','), i.metadata->>'type')
LIMIT $3
"""


def _embedding_base_url() -> str:
    """Normalize the embedding base URL to carry the /v1 API path.

    The openai SDK 2.x turns a bare host into a plain-text response (no `.data`),
    so every client must pass the /v1 form (mirrors llm_base_url_v1 / lightrag_init).
    """
    base = (settings.embedding_base_url or "").strip()
    return base if base.rstrip("/").endswith("/v1") else base.rstrip("/") + "/v1"


async def _embed_query(text: str) -> list[float]:
    """Return the 1024-dim embedding for a query string (LOCK: text-embedding-v4)."""
    import openai

    client = openai.AsyncOpenAI(
        api_key=settings.embedding_api_key, base_url=_embedding_base_url()
    )
    resp = await client.embeddings.create(model=settings.embedding_model, input=[text])
    # Sort by index for stability; the batch API may reorder responses.
    ordered = sorted(resp.data, key=lambda d: d.index)
    return [float(x) for x in ordered[0].embedding]


def _normalize_unit_code(code: str) -> str | None:
    """Return the canonical zero-padded uppercase unit (CH-3 -> CH-03, CH-3A -> CH-03A).

    The DB stores unit keys zero-padded (unit:CH-03) while users type short forms
    (CH-3). Collapsing both onto one canonical form is what lets an exact match
    succeed, so every comparison must go through this function.
    """
    m = re.fullmatch(r"CH-(\d{1,2})([A-Z]?)", code.strip().upper())
    if not m:
        return None
    num, suffix = m.groups()
    return f"CH-{int(num):02d}{suffix}"


def _extract_unit_codes(query_text: str) -> list[str]:
    """Return every unit code in the query (e.g. CH-03, CH-03A) in canonical form.

    Empty when the query names no concrete unit, in which case the caller must
    fall back to plain semantic ranking because there is no exact target to honor.
    """
    return [
        normalized
        for m in _UNIT_CODE_RE.findall(query_text)
        if (normalized := _normalize_unit_code(m)) is not None
    ]


def _meta_of(row: dict[str, Any]) -> dict[str, Any]:
    """Return the row's metadata dict, tolerating either a JSONB dict or a JSON string."""
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return {}
    return meta if isinstance(meta, dict) else {}


def _row_unit(row: dict[str, Any]) -> str | None:
    """Return the normalized unit code for a row, or None if it has none."""
    link = row.get("linked_subject_key")
    if isinstance(link, str) and link:
        # 'unit:CH-03' -> 'CH-03', but tolerate any prefix before the code.
        value = link.rsplit(":", 1)[-1]
        if value:
            return _normalize_unit_code(value)
    unit = _meta_of(row).get("unit")
    return _normalize_unit_code(str(unit)) if unit else None


def _row_type(row: dict[str, Any]) -> str | None:
    """Return the unit type (e.g. 3PN, Studio) from metadata, or None if absent."""
    value = _meta_of(row).get("type")
    return str(value) if value else None


def _row_to_image(
    row: dict[str, Any], score: float, match: str, reason: str | None
) -> dict[str, Any]:
    """Shape a DB row into the stable image contract plus match/reason labels."""
    return {
        "image_id": row.get("image_id"),
        "kind": row.get("kind"),
        "title": row.get("title"),
        "caption": row.get("caption"),
        "alt_text": row.get("alt_text"),
        "url_cdn": row.get("url_cdn"),
        "width": row.get("width"),
        "height": row.get("height"),
        "score": score,
        "match": match,
        "reason": reason,
    }


async def _query_by_unit(code: str) -> dict[str, Any] | None:
    """Fetch the single image whose linked unit key equals code; None on miss."""
    try:
        async with with_rls_identity() as conn:
            rec = await conn.fetchrow(QUERY_BY_UNIT, f"unit:{code}", code)
        return dict(rec) if rec is not None else None
    except Exception as exc:  # noqa: BLE001 — degrade silently; exact rescue is best-effort
        logger.warning("image_search: unit lookup failed for %s: %s", code, exc)
        return None


async def _rerank_by_unit(
    target_codes: set[str], scored: list[tuple[dict[str, Any], float]], top_k: int
) -> list[dict[str, Any]]:
    """Re-order vector hits so exact unit matches lead, same-type units follow.

    Exact is decided on the normalized unit code only (never on caption overlap):
    a user who names CH-03 expects that floor-plan, not a different 3PN. When the
    vector pass missed the exact unit we rescue it with a direct index lookup and
    prepend it. Leftover hits (different unit or no unit) that share the queried
    unit's type are kept for visual comparison; unrelated hits stay at the tail.
    """
    exact: list[tuple[dict[str, Any], float]] = []
    leftover: list[tuple[dict[str, Any], float]] = []
    for row, score in scored:
        unit = _row_unit(row)
        (exact if unit and unit in target_codes else leftover).append((row, score))

    found = {_row_unit(row) for row, _ in exact}
    for code in sorted(target_codes - found):
        rescued = await _query_by_unit(code)
        if rescued is not None:
            exact.append((rescued, 1.0))  # ceiling: exact-intent surrogate for a direct hit

    # Same-type neighbors are only "similar" relative to the exact unit(s) found.
    ref_types: set[str] = {t for t in (_row_type(row) for row, _ in exact) if t}
    similar: list[tuple[dict[str, Any], float]] = []
    semantic: list[tuple[dict[str, Any], float]] = []
    for row, score in leftover:
        unit_type = _row_type(row)
        (similar if unit_type and unit_type in ref_types else semantic).append((row, score))

    ordered = exact + similar + semantic
    out: list[dict[str, Any]] = []
    for row, score in ordered:
        unit = _row_unit(row)
        if unit and unit in target_codes:
            out.append(_row_to_image(row, score, "exact", f"Đúng căn {unit} bạn hỏi"))
            continue
        unit_type = _row_type(row)
        if unit_type and unit_type in ref_types:
            out.append(_row_to_image(row, score, "similar", f"Căn {unit_type} tương tự để so sánh"))
            continue
        out.append(_row_to_image(row, score, "semantic", None))
    return out[:top_k]


async def search_project_images(
    top_k: int = 6, kind: str = "matbang"
) -> list[dict[str, Any]]:
    """Return representative project imagery for the first-open greeting.

    A welcome message should show the project, not a specific floor plan, so we
    pick published images of the given kind that have NO unit link (they are
    overviews: cover, render, amenity map/collage) and deterministically order
    them by the display type so the greeting always leads with the most visual
    asset. Best-effort: any DB failure returns [] so the greeting never 500s.
    """
    order = ["cover", "render", "amenity_map", "amenity_collage"]
    try:
        async with with_rls_identity() as conn:
            recs = await conn.fetch(
                PROJECT_IMAGES_QUERY,
                kind,
                ",".join(order),
                top_k,
            )
    except Exception as exc:  # noqa: BLE001 — greeting imagery is a garnish, never fatal
        logger.warning("image_search: project images degraded: %s", exc)
        return []
    rows = [dict(r) for r in recs]
    # The query already orders by array_position over the display-type preference
    # list, so no client-side re-sort is needed; unit-linked rows are dropped below.
    out: list[dict[str, Any]] = []
    for r in rows:
        if _row_unit(r):
            continue
        out.append(_row_to_image(r, 1.0, "semantic", None))
    return out[:top_k]


async def search_images(
    query_text: str,
    top_k: int = 4,
    threshold: float = 0.45,
    margin: float | None = None,
    same_kind_margin: float = 0.15,
    cross_kind_margin: float = 0.05,
) -> list[dict[str, Any]]:
    """Return up to top_k published illustrative images that pass a relevance gate.

    Two layers keep the gallery semantically tied to the question, so a query that
    has no matching image returns nothing instead of a best-effort floor plan:

    - ``threshold`` is an absolute floor on the caption-embedding cosine score.
      Cross-topic pairs that only share project context ("The Camellia Sơn Trà",
      "căn hộ") measure 0.40-0.46 with text-embedding-v4, so the old 0.4 floor let
      unrelated tail images attach to any query. 0.45 rejects those while keeping
      every genuinely topical cluster (payment 0.56+, floor plan 0.50+, price 0.62+).
    - ``same_kind_margin`` / ``cross_kind_margin`` are relative gates against the
      top hit, split by whether a candidate shares the top hit's ``kind``. One
      scalar margin cannot both keep a full topical cluster and reject an
      off-topic tail, because the two score ranges overlap: the four payment-method
      images span 0.4586-0.5615 (widest same-kind gap 0.1029), while a floor-plan
      at 0.509 sits only 0.104 below a 0.613 payment hit. ``same_kind_margin``
      (0.15) is wide enough to hold the whole payment cluster; ``cross_kind_margin``
      (0.05) is tight enough to drop the floor-plan. The legacy ``margin`` argument
      is kept for back-compat: when passed, the single scalar drives both windows
      (exactly the old behavior).

    The vector pass fetches a candidate pool larger than ``top_k`` so both gates
    choose from a fuller picture before the final top_k slice. When the query names
    concrete unit code(s), results are re-ranked so the exact unit(s) lead and
    same-type units follow (exact rescues deliberately bypass the floor); otherwise
    raw score order is kept. Degrades to [] on any embedding or DB error: image
    retrieval is a garnish to the answer, never a reason to fail the pipeline.
    """
    if not query_text:
        return []
    try:
        vector = await _embed_query(query_text)
        # asyncpg cannot encode a bare float list as an hstore-free PG vector, so
        # the literal is passed as text and cast server-side.
        vec_literal = str([float(x) for x in vector])
        # Fetch a superset of the final count so the floor/margin gates are not
        # starved by the LIMIT; a topical cluster that lands just outside the top_k
        # raw neighbors still gets a chance to pass the gates.
        pool = max(top_k, 8)
        async with with_rls_identity() as conn:
            recs = await conn.fetch(IMAGE_QUERY, vec_literal, pool)
        rows = [dict(r) for r in recs]
    except Exception as exc:  # noqa: BLE001 — image search failure never crashes the pipeline
        logger.warning("image_search: degraded (no images): %s", exc)
        return []

    scored: list[tuple[dict[str, Any], float]] = []
    for r in rows:
        score = r.get("score")
        if score is None or float(score) < threshold:
            continue
        scored.append((r, float(score)))

    codes = _extract_unit_codes(query_text)
    if not codes:
        if not scored:
            return []
        # No concrete unit target: plain semantic ranking is the honest answer,
        # gated per kind so the gallery keeps the full topical cluster yet never
        # trails into images of a different kind that merely share project
        # vocabulary with the top match (the score ranges overlap across kinds).
        top_score = scored[0][1]
        top_kind = scored[0][0].get("kind")
        # Legacy scalar override: one margin for both windows keeps old behavior.
        if margin is not None:
            same_kind_margin = margin
            cross_kind_margin = margin
        kept: list[tuple[dict[str, Any], float]] = []
        for r, s in scored:
            # Inclusive bound: a gap exactly equal to the window is kept; anything
            # beyond it by even float noise is dropped.
            gap = top_score - s
            window = same_kind_margin if r.get("kind") == top_kind else cross_kind_margin
            if gap > window:
                continue
            kept.append((r, s))
        return [_row_to_image(r, s, "semantic", None) for r, s in kept][:top_k]

    return await _rerank_by_unit(set(codes), scored, top_k)
