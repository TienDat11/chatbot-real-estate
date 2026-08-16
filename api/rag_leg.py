"""RAG leg — LightRAG hybrid retrieval with post-filter for document validity.

Retrieves context via `get_lightrag().aquery_data(...)`, then filters every chunk
against the documents registry (status='published' + effective interval at as_of)
so expired legal texts never reach the LLM. Timeout/error degrades gracefully.

1.5.6 note: `aquery()` is a backward-compat wrapper that returns only the LLM
response string — the structured retrieval result (entities/chunks with
file_path) lives in `aquery_data()`, which we call here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from api import get_cfg
from api.constants import (
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
)
from api.sql_leg import get_ro_pool

logger = logging.getLogger("api.rag_leg")

# Flag for GET /ready (main.py) — set once get_lightrag succeeds.
LIGHTRAG_READY = False

# PG storages initialized once per process (ingest side uses the same helper).
_api_storages_ready = False


@dataclass
class RagLegResult:
    chunks: list[dict] = field(default_factory=list)  # [{id, score, content, doc_id, section, ...}]
    degraded: bool = False
    error: str | None = None


def _get_rag_budget() -> tuple[int, int, int]:
    """max entity/relation/total tokens — prefers settings.rag_* over query_max_*."""
    ent = get_cfg("rag_max_entity_tokens", get_cfg("query_max_entity_tokens", DEFAULT_MAX_ENTITY_TOKENS))
    rel = get_cfg("rag_max_relation_tokens", get_cfg("query_max_relation_tokens", DEFAULT_MAX_RELATION_TOKENS))
    tot = get_cfg("rag_max_total_tokens", get_cfg("query_max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS))
    return int(ent or DEFAULT_MAX_ENTITY_TOKENS), int(rel or DEFAULT_MAX_RELATION_TOKENS), int(tot or DEFAULT_MAX_TOTAL_TOKENS)


async def _get_rag():
    """Lazy LightRAG instance — accepts both sync and async factories (defensive).

    1.5.6: aquery_data raises PipelineNotInitializedError until initialize_storages
    has run in this process, so the query side mirrors the ingest side's flag once.
    """
    global LIGHTRAG_READY, _api_storages_ready
    from ingest.lightrag_init import get_lightrag  # noqa: PLC0415

    rag = get_lightrag()
    if inspect.isawaitable(rag):
        rag = await rag
    if not _api_storages_ready:
        await rag.initialize_storages()
        _api_storages_ready = True
    LIGHTRAG_READY = True
    return rag


def _make_query_param(hl: list[str], ll: list[str]) -> Any:
    """Build a LightRAG QueryParam — defensive if kwargs change across versions."""
    from lightrag.lightrag import QueryParam  # noqa: PLC0415

    ent, rel, tot = _get_rag_budget()
    try:
        return QueryParam(
            mode="hybrid",
            only_need_context=True,
            hl_keywords=hl or None,
            ll_keywords=ll or None,
            enable_rerank=False,  # LightRAG does not rerank; app-side rerank owns scores
            max_entity_tokens=ent,
            max_relation_tokens=rel,
            max_total_tokens=tot,
            addon_params={
                "language": "Vietnamese",
                "entity_type_prompt_file": "prompts/entity_type/legal_vn.yml",
            },
            entity_extraction_use_json=True,
        )
    except TypeError as exc:  # older version missing kwargs -> minimal set
        logger.warning("QueryParam full kwargs fail (%s) — fallback minimal", exc)
        return QueryParam(
            mode="hybrid", only_need_context=True, hl_keywords=hl or None, ll_keywords=ll or None,
            enable_rerank=False,
        )


def _normalize_chunks(raw_chunks: list | None) -> list[dict]:
    """Normalize aquery_data chunks to {id, score, content, file_path} dicts.

    The registry joins on document_chunks.chunk_id, which equals the ingest
    leg's file_path (ids/file_paths 1:1). The 1.5.6 chunk key ('<file_path>-chunk-000')
    must NOT be used as id or the post-filter drops every chunk.
    """
    out: list[dict] = []
    for c in raw_chunks or []:
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "id": c.get("file_path") or c.get("id") or c.get("chunk_id"),
                "score": float(c.get("score", 0.0) or 0.0),
                "content": c.get("content", "") or "",
                "file_path": c.get("file_path"),
            }
        )
    return out


async def _post_filter(chunks: list[dict], as_of: date | None) -> list[dict]:
    """Drop chunks whose doc is not published or not effective at as_of."""
    if not chunks:
        return []
    ids = [c["id"] for c in chunks if c.get("id")]
    if not ids:
        return []
    as_of = as_of or date.today()  # as_of=None -> now
    pool = await get_ro_pool()
    sql = """SELECT c.chunk_id, d.doc_id, d.status, d.effective_from, d.effective_to
             FROM document_chunks c
             JOIN documents d ON d.doc_id = c.doc_id
             WHERE c.chunk_id = ANY($1)"""
    async with pool.acquire() as conn:
        recs = await conn.fetch(sql, ids)
    valid: set[str] = set()
    for r in recs:
        if r["status"] != "published":
            continue
        if r["effective_from"] and r["effective_from"] > as_of:
            continue
        if r["effective_to"] and as_of is not None and r["effective_to"] <= as_of:
            continue
        valid.add(r["chunk_id"])
    return [c for c in chunks if c.get("id") in valid]


async def run_rag_leg(rewritten: str, hl: list[str], ll: list[str], as_of: date | None) -> RagLegResult:
    """Run LightRAG hybrid + validity post-filter; any error degrades (never crashes)."""
    # 1. Get instance (lazy) — first call also runs initialize_storages (0.3-3s).
    try:
        rag = await asyncio.wait_for(_get_rag(), timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_leg: lightrag init failed: %s", exc)
        return RagLegResult([], degraded=True, error=f"lightrag init: {exc}")

    # 2. Build QueryParam.
    try:
        qparam = _make_query_param(hl, ll)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_leg: QueryParam build failed: %s", exc)
        return RagLegResult([], degraded=True, error=f"QueryParam: {exc}")

    # 3. aquery_data — structured retrieval (1.5.6's aquery() returns only the LLM
    #    string; chunks + file_paths live here). Generation is a separate step.
    try:
        result = await asyncio.wait_for(rag.aquery_data(rewritten, param=qparam), timeout=15.0)
    except Exception as exc:  # noqa: BLE001 — timeout/provider error -> degrade
        logger.warning("rag_leg: aquery_data failed: %s", exc)
        return RagLegResult([], degraded=True, error=f"aquery_data: {exc}")

    payload = result.get("data") or {} if isinstance(result, dict) else {}
    chunks = _normalize_chunks(payload.get("chunks"))
    if not chunks:
        return RagLegResult([], degraded=False, error=None)  # no chunks — not an error

    # 4. Validity post-filter (registry JOIN).
    try:
        kept = await asyncio.wait_for(_post_filter(chunks, as_of), timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_leg: post-filter failed: %s", exc)
        return RagLegResult([], degraded=True, error=f"post-filter: {exc}")

    return RagLegResult(chunks=kept, degraded=False, error=None)
