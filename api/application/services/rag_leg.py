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
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from api import get_cfg
from api.domain.value_objects.constants import (
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
)

from .sql_leg import get_ro_pool

logger = logging.getLogger("api.rag_leg")

# Flag for GET /ready (main.py) — set once get_lightrag succeeds.
LIGHTRAG_READY = False

# PG storages initialized once per process (ingest side uses the same helper).
_api_storages_ready_workspaces: set[str] = set()


@dataclass
class RagLegResult:
    chunks: list[dict] = field(default_factory=list)  # [{id, score, content, doc_id, section, ...}]
    degraded: bool = False
    error: str | None = None
    degraded_reasons: tuple[str, ...] = ()


# One-attempt budget (seconds) for rag.aquery_data. The env var must win even
# when the lru-cached Settings are already warm (reload/tests mutate env after
# first import), so os.getenv takes explicit precedence over get_cfg; the
# default lives in Settings (180s, raised from the old hard-coded 15s). The
# retry-once-on-timeout behavior at the call site is unchanged.
AQUERY_TIMEOUT_S = float(os.getenv("RAG_AQUERY_TIMEOUT_S") or get_cfg("rag_aquery_timeout_s", 180.0))


def _get_rag_budget() -> tuple[int, int, int]:
    """max entity/relation/total tokens — prefers settings.rag_* over query_max_*."""
    ent = get_cfg(
        "rag_max_entity_tokens", get_cfg("query_max_entity_tokens", DEFAULT_MAX_ENTITY_TOKENS)
    )
    rel = get_cfg(
        "rag_max_relation_tokens", get_cfg("query_max_relation_tokens", DEFAULT_MAX_RELATION_TOKENS)
    )
    tot = get_cfg(
        "rag_max_total_tokens", get_cfg("query_max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS)
    )
    return (
        int(ent or DEFAULT_MAX_ENTITY_TOKENS),
        int(rel or DEFAULT_MAX_RELATION_TOKENS),
        int(tot or DEFAULT_MAX_TOTAL_TOKENS),
    )


def _project_workspace(project_key: str | None) -> str | None:
    """Delegate to the ingest-side mapping (single source of truth)."""
    from ingest.lightrag_init import project_workspace  # noqa: PLC0415

    return project_workspace(project_key)


async def _get_rag(project_key: str | None = None):
    """Lazy LightRAG instance for the project's workspace — sync/async factory safe.

    1.5.6: aquery_data raises PipelineNotInitializedError until initialize_storages
    has run in this process, so the query side mirrors the ingest side's flag once
    per workspace instance.
    """
    global LIGHTRAG_READY, _api_storages_ready_workspaces
    from ingest.lightrag_init import get_lightrag  # noqa: PLC0415

    workspace = _project_workspace(project_key)
    rag = get_lightrag(workspace)
    if inspect.isawaitable(rag):
        rag = await rag
    if workspace not in _api_storages_ready_workspaces:
        await rag.initialize_storages()
        _api_storages_ready_workspaces.add(workspace or "")
    LIGHTRAG_READY = True
    return rag


def _make_query_param(hl: list[str], ll: list[str]) -> Any:
    """Build a LightRAG QueryParam — defensive if kwargs change across versions."""
    from lightrag.lightrag import QueryParam  # noqa: PLC0415

    ent, rel, tot = _get_rag_budget()
    # Query mode is settings-driven (RAG_QUERY_MODE; validator clamps the set),
    # so retrieval behavior is tunable per environment without a code change.
    query_mode = str(get_cfg("rag_query_mode", "hybrid"))
    try:
        return QueryParam(
            mode=query_mode,  # type: ignore[arg-type]  # validated in Settings
            only_need_context=True,
            hl_keywords=hl or None,
            ll_keywords=ll or None,
            enable_rerank=False,  # LightRAG does not rerank; app-side rerank owns scores
            max_entity_tokens=ent,
            max_relation_tokens=rel,
            max_total_tokens=tot,
            addon_params={
                "language": "Vietnamese",
                "entity_type_prompt_file": "legal_vn.yml",  # LightRAG 1.5.6: bare name under PROMPT_DIR/entity_type (HF-0)  # noqa: E501
            },
            entity_extraction_use_json=True,
        )
    except TypeError as exc:  # older version missing kwargs -> minimal set
        logger.warning("QueryParam full kwargs fail (%s) — fallback minimal", exc)
        return QueryParam(
            mode=query_mode,  # type: ignore[arg-type]  # validated in Settings
            only_need_context=True,
            hl_keywords=hl or None,
            ll_keywords=ll or None,
            enable_rerank=False,
        )


def _normalize_chunks(raw_chunks: list | None) -> list[dict]:
    """Normalize aquery_data chunks and retain an explicit missing-id marker."""
    out: list[dict] = []
    for c in raw_chunks or []:
        if not isinstance(c, dict):
            continue
        chunk_id = c.get("file_path") or c.get("id") or c.get("chunk_id")
        out.append(
            {
                "id": chunk_id,
                "score": float(c.get("score", 0.0) or 0.0),
                "content": c.get("content", "") or "",
                "file_path": c.get("file_path"),
                "_missing_chunk_id": not bool(chunk_id),
            }
        )
    return out


async def _post_filter(
    chunks: list[dict],
    as_of: date | None,
    project_key: str | None = None,
) -> list[dict]:
    """Drop chunks whose doc is not published or not effective at as_of.

    ``project_key`` (story 10.4): the documents.project_key column is the
    per-project tag from ISSUE-01, so scoping here is a plain column filter —
    a Soleil query never keeps Camellia chunks even if LightRAG (which has no
    workspace filter in 1.5.6) surfaces them.

    Training turns are NOT a separate filter layer any more: by business
    decision the training corpus IS the project corpus (kind is only ever
    legal|price|project), so a training turn rides this exact project filter
    with its real context project key — project isolation stays absolute and
    no document kind is ever required or excluded.
    """
    if not chunks:
        return []
    ids = [c["id"] for c in chunks if c.get("id")]
    if not ids:
        return []
    as_of = as_of or date.today()  # as_of=None -> now
    pool = await get_ro_pool()
    sql = """SELECT c.chunk_id, d.doc_id, d.status, d.effective_from, d.effective_to,
                    d.project_key, d.kind
             FROM document_chunks c
             JOIN documents d ON d.doc_id = c.doc_id
             WHERE c.chunk_id = ANY($1)"""
    args: list[Any] = [ids]
    if project_key:
        # Chunks whose doc carries the project tag; NULL-tagged docs are excluded
        # so an untagged legacy doc never leaks into a project's answer.
        sql += " AND d.project_key = $2"
        args.append(project_key)
    async with pool.acquire() as conn:
        recs = await conn.fetch(sql, *args)
    valid: set[str] = set()
    for r in recs:
        if r["status"] != "published":
            continue
        if project_key and r["project_key"] != project_key:
            continue
        if r["effective_from"] and r["effective_from"] > as_of:
            continue
        if r["effective_to"] and as_of is not None and r["effective_to"] <= as_of:
            continue
        valid.add(r["chunk_id"])
    return [c for c in chunks if c.get("id") in valid]


async def run_rag_leg(
    rewritten: str,
    hl: list[str],
    ll: list[str],
    as_of: date | None,
    project_key: str | None = None,
) -> RagLegResult:
    """Run LightRAG hybrid + validity post-filter; any error degrades (never crashes).

    ``project_key`` is the absolute corpus scope — training turns pass their
    real context project here (never a training pseudo-key), so retrieval and
    the registry post-filter behave identically to customer mode.
    """
    # 1. Get instance (lazy) — first call also runs initialize_storages (0.3-3s).
    #    20s cap: cold processes measured past the old 15s budget; a cancelled
    #    init degraded silently because str(asyncio.TimeoutError) is EMPTY, so
    #    every degrade message now carries the exception class name (Bug B
    #    observability: "rag init" was invisible in flags/audit).
    try:
        rag_candidate = _get_rag(project_key)
        rag = (
            await asyncio.wait_for(rag_candidate, timeout=20.0)
            if inspect.isawaitable(rag_candidate)
            else rag_candidate
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_leg: lightrag init failed: %s: %s", type(exc).__name__, exc)
        return RagLegResult(
            [],
            degraded=True,
            error=f"lightrag init: {type(exc).__name__}: {exc}",
            degraded_reasons=("rag_init_error",),
        )

    # 2. Build QueryParam.
    try:
        qparam = _make_query_param(hl, ll)
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_leg: QueryParam build failed: %s", exc)
        return RagLegResult(
            [],
            degraded=True,
            error=f"QueryParam: {exc}",
            degraded_reasons=("query_param_error",),
        )

    # 3. aquery_data — structured retrieval (1.5.6's aquery() returns only the LLM
    #    string; chunks + file_paths live here). Retry exactly once on timeout;
    #    provider errors are not retried because they may be deterministic.
    result: Any = None
    for attempt in range(2):
        try:
            result = await asyncio.wait_for(
                rag.aquery_data(rewritten, param=qparam), timeout=AQUERY_TIMEOUT_S
            )
            break
        except asyncio.TimeoutError as exc:
            if attempt == 0:
                logger.warning("rag_leg: aquery_data timed out; retrying once")
                continue
            logger.warning("rag_leg: aquery_data failed: %s: %s", type(exc).__name__, exc)
            return RagLegResult(
                [],
                degraded=True,
                error=f"aquery_data: {type(exc).__name__}: {exc}",
                degraded_reasons=("aquery_timeout",),
            )
        except Exception as exc:  # noqa: BLE001 — provider error -> degrade, no retry
            logger.warning("rag_leg: aquery_data failed: %s: %s", type(exc).__name__, exc)
            return RagLegResult(
                [],
                degraded=True,
                error=f"aquery_data: {type(exc).__name__}: {exc}",
                degraded_reasons=("aquery_error",),
            )

    payload = result.get("data") or {} if isinstance(result, dict) else {}
    chunks = _normalize_chunks(payload.get("chunks"))
    missing_ids = sum(1 for chunk in chunks if chunk.get("_missing_chunk_id"))
    if not chunks:
        return RagLegResult([], degraded=False, error=None)

    # 4. Validity + project post-filter (registry JOIN). Chunks without an ID
    #    cannot be safely joined and are therefore deliberately excluded.
    try:
        kept = await asyncio.wait_for(
            _post_filter(chunks, as_of, project_key), timeout=1.5
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rag_leg: post-filter failed: %s", exc)
        reasons = (
            ("chunks_missing_ids", "post_filter_error") if missing_ids else ("post_filter_error",)
        )
        return RagLegResult(
            [], degraded=True, error=f"post-filter: {exc}", degraded_reasons=reasons
        )

    reasons: list[str] = []
    if missing_ids:
        reasons.append("chunks_missing_ids")
    if len(kept) < len(chunks) - missing_ids:
        reasons.append("chunks_filtered")
    return RagLegResult(
        chunks=kept, degraded=bool(reasons), error=None, degraded_reasons=tuple(reasons)
    )
