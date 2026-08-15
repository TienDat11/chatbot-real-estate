"""LightRAG singleton — PG storages, embedding binding, chunking passthrough.

(plan §3.7 A1 + §4.3 A3) Vietnamese embeddings, entity_type_prompt_file, entity_extraction_use_json.
(spike day 1) Verify the LightRAG 1.5.6 signature — constructor kwargs, chunking_func, and the
QueryParam import path. The code below is defensive: it tries several import paths and falls back.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from api.config import export_runtime_env
from ingest.config import settings

# LightRAG PG storages read POSTGRES_* from the process env, not from Settings.
export_runtime_env(settings)

logger = logging.getLogger(__name__)

_lightrag: Any | None = None
_lock = threading.Lock()
_storages_initialized = False
LIGHTRAG_READY = False  # read by api/rag_leg.py and /ready


def _redact(exc: Exception) -> str:
    """Strip secrets from an exception string before it reaches logs."""
    msg = str(exc)
    for secret in (settings.llm_api_key, settings.embedding_api_key):
        if secret:
            msg = msg.replace(secret, "***")
    return msg


class LightRAGUnavailableError(RuntimeError):
    """LightRAG could not initialize (missing dep/key) — let the pipeline degrade, not crash."""


def _make_embedding_func() -> Any:
    """EmbeddingFunc for the configured binding (dims LOCK: 1024).

    (spike 2, provider) lightrag-hku 1.5.6 requires an EmbeddingFunc dataclass whose
    `.func` is an async callable; the dimension travels via the wrapper (the
    constructor no longer takes embedding_dim).
    """
    import numpy as np
    import openai

    try:
        from lightrag.utils import EmbeddingFunc
    except ImportError as exc:  # pragma: no cover — environment-dependent
        raise LightRAGUnavailableError(f"Thiếu lightrag-hku==1.5.6: {exc}") from exc

    if settings.embedding_binding in ("dashscope", "aibox") and settings.embedding_api_key:
        # openai SDK 2.x turns a bare host into a plain-text response (no .data),
        # so the base URL must carry the /v1 path like llm_base_url_v1 does.
        base = settings.embedding_base_url.strip()
        embedding_http_base = base if base.rstrip("/").endswith("/v1") else base.rstrip("/") + "/v1"
        client = openai.AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=embedding_http_base,
        )
        model = settings.embedding_model

        async def embed(texts: list[str]) -> np.ndarray:
            resp = await client.embeddings.create(model=model, input=texts)
            # Sort by index for stability (the batch API may reorder responses).
            ordered = sorted(resp.data, key=lambda d: d.index)
            # float32 ndarray: EmbeddingFunc.__call__ validates via .size and the
            # PG vector storage encodes float32 — a plain list would crash the
            # flush ('list' object has no attribute 'size').
            return np.asarray([d.embedding for d in ordered], dtype=np.float32)

        return EmbeddingFunc(embedding_dim=settings.embedding_dim, func=embed, model_name=model)

    # A real binding with no API key must fail closed — the stub writes garbage
    # vectors that silently corrupt the store. The stub is reachable ONLY through
    # an explicit EMBEDDING_BINDING=local.
    if settings.embedding_binding != "local":
        raise LightRAGUnavailableError(
            f"EMBEDDING_BINDING={settings.embedding_binding!r} cần embedding_api_key — "
            "fail closed (stub local chỉ dùng khi binding=local)"
        )
    logger.warning(
        "EMBEDDING_BINDING=local — dùng stub (vector rác). KHÔNG được chạy production "
        "với stub này. SPIKE: cài model local.",
    )
    return _local_embedding_fallback()


def _local_embedding_fallback() -> Any:
    """Return a stub EmbeddingFunc (vector rác — dev-only, production phải dùng binding thật)."""
    import numpy as np
    from lightrag.utils import EmbeddingFunc

    async def local_embed(texts: list[str]) -> np.ndarray:
        rng = np.random.default_rng(0)
        return np.asarray(
            [list(rng.random(settings.embedding_dim).astype(float)) for _ in texts],
            dtype=np.float32,
        )

    return EmbeddingFunc(
        embedding_dim=settings.embedding_dim, func=local_embed, model_name="local-stub"
    )


def _make_llm_func() -> Callable[..., Any]:
    """LLM for LightRAG entity/relation extraction — qwen3.7-flash, async OpenAI-compatible."""
    import openai

    client = openai.AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url_v1)
    model = settings.llm_model_extract

    async def llm_func(prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        return resp.choices[0].message.content or ""

    return llm_func


def get_lightrag() -> Any:
    """Singleton LightRAG với PG storages. Init lần đầu (lazy) — thread-safe."""
    global _lightrag, LIGHTRAG_READY
    if _lightrag is not None:
        return _lightrag

    with _lock:
        if _lightrag is not None:
            return _lightrag

        try:
            from lightrag import LightRAG, QueryParam  # noqa: F401  (re-exported for shared use)
        except ImportError as exc:  # pragma: no cover — environment-dependent
            raise LightRAGUnavailableError(
                f"Thiếu lightrag-hku==1.5.6 (pip install -r requirements.txt): {exc}"
            ) from exc

        # (spike 4) Constructor signature verified against the installed 1.5.6
        #   wheel: storages are selected by NAME via lightrag.kg.factory; dims
        #   travel on the EmbeddingFunc, not an `embedding_dim` kwarg; graph
        #   storage must be PGTableGraphStorage — PGGraphStorage wraps Apache AGE
        #   (create_graph), which managed PG forbids (CLAUDE.md ADR-001). The JSON
        #   extraction toggle is a TOP-LEVEL ctor field; `language` lives in
        #   addon_params. ainsert passthrough: sections are raw-splitted per
        #   chunk_token_size, so pre-chunked sections stay one chunk each.
        try:
            _lightrag = LightRAG(
                working_dir=settings.lightrag_workspace,
                embedding_func=_make_embedding_func(),
                llm_model_func=_make_llm_func(),
                llm_model_name=settings.llm_model_extract,
                llm_model_kwargs={
                    "base_url": settings.llm_base_url_v1,
                    "api_key": settings.llm_api_key,
                },
                kv_storage="PGKVStorage",
                doc_status_storage="PGDocStatusStorage",
                graph_storage="PGTableGraphStorage",
                vector_storage="PGVectorStorage",
                entity_extraction_use_json=True,
                addon_params={
                    "language": "Vietnamese",
                },
                chunk_token_size=settings.chunk_cap,
                chunk_overlap_token_size=50,
                enable_llm_cache=True,
                max_parallel_insert=settings.max_parallel_workers,
            )
        except Exception as exc:  # pragma: no cover — environment-dependent
            raise LightRAGUnavailableError(
                f"LightRAG init fail (lightrag-hku): {_redact(exc)}"
            ) from exc

        logger.info(
            "LightRAG sẵn sàng (workspace=%s, binding=%s)",
            settings.lightrag_workspace,
            settings.embedding_binding,
        )
        LIGHTRAG_READY = True
        return _lightrag


async def _ensure_lightrag_ready(rag: Any) -> None:
    """Initialize the 1.5.6 pipeline/storage DDL once per LightRAG lifetime.

    ainsert/adelete on the installed wheel raise PipelineNotInitializedError until
    `await rag.initialize_storages()` has run; the flag keeps it to one call.
    """
    global _storages_initialized
    if not _storages_initialized:
        await rag.initialize_storages()
        _storages_initialized = True


async def ainsert_document(rag: Any, doc_id: str, chunks: list[str], chunk_ids: list[str]) -> None:
    """Insert one document into LightRAG — each section is its own Raw doc.

    (plan §3.2 step 6) Insert after COMMIT. `ainsert` with `ids` takes the SDK
    raw direct-insert path: every section becomes one LightRAG document whose
    id (and file_path) is the registry chunk_id (`doc_id:version:index`), so the
    vector store carries the registry id verbatim and the F chunker leaves the
    pre-chunked section as a single chunk (chunk key `{chunk_id}-chunk-000`).
    file_path must be unique per section or the 1.5.6 filename dedup drops
    sections 2..n of a multi-chunk document.
    """
    try:
        await _ensure_lightrag_ready(rag)
        await rag.ainsert(
            chunks,
            ids=chunk_ids,
            file_paths=chunk_ids,
        )
    except Exception as exc:  # noqa: BLE001 — surface the error; callers decide on retry
        raise RuntimeError(f"ainsert LightRAG lỗi (doc={doc_id}): {_redact(exc)}") from exc


async def adelete_by_doc_id(rag: Any, lightrag_doc_id: str) -> None:
    """Remove a document from LightRAG on doc invalidation (plan §3.6)."""
    try:
        await _ensure_lightrag_ready(rag)
        await rag.adelete_by_doc_id(lightrag_doc_id)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"adelete LightRAG lỗi (id={lightrag_doc_id}): {_redact(exc)}") from exc
