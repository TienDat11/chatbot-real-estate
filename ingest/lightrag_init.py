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
LIGHTRAG_READY = False  # read by api/rag_leg.py and /ready


class LightRAGUnavailableError(RuntimeError):
    """LightRAG could not initialize (missing dependency/key) — let the pipeline degrade, not crash."""


def _make_embedding_func() -> Callable[[list[str]], list[list[float]]]:
    """Return the embedding function for the configured binding.

    (spike 2, provider) Verify the real base URL and max_token of text-embedding-v4.
    """
    import openai

    if settings.embedding_binding in ("dashscope", "aibox") and settings.embedding_api_key:
        client = openai.OpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
        model = settings.embedding_model

        def embed(texts: list[str]) -> list[list[float]]:
            resp = client.embeddings.create(model=model, input=texts)
            # Sort by index for stability (the batch API may reorder responses).
            ordered = sorted(resp.data, key=lambda d: d.index)
            return [d.embedding for d in ordered]

        return embed

    # Fallback 'local' — a real local model (Qwen3-Embedding-0.6B, 1024 dims) is required.
    logger.warning(
        "EMBEDDING_BINDING=%r không có key/không hỗ trợ — dùng stub local. "
        "KHÔNG được chạy production với stub này (vector rác). SPIKE: cài model local.",
        settings.embedding_binding,
    )

    import numpy as np

    def local_embed(texts: list[str]) -> list[list[float]]:
        rng = np.random.default_rng(0)
        return [list(rng.random(settings.embedding_dim).astype(float)) for _ in texts]

    return local_embed


def _make_llm_func() -> Callable[..., Any]:
    """LLM for LightRAG extraction (graph entities/relations) — qwen3.7-flash over an OpenAI-compatible client."""
    import openai

    client = openai.OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    model = settings.llm_model_extract

    def llm_func(prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
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
            from lightrag.lightrag import QueryParam as _QP  # correct-path attempt (spike)
            from lightrag.storage import (
                PGKVStorage,
                PGDocStatusStorage,
                PGTableGraphStorage,
                PGVectorStorage,
            )
        except ImportError as exc:  # pragma: no cover — environment-dependent
            raise LightRAGUnavailableError(
                f"Thiếu lightrag-hku==1.5.6 (pip install -r requirements.txt): {exc}"
            ) from exc

        # (spike 4) Verify the exact ainsert chunking_func and QueryParam signatures.
        #   Passthrough returns chunks as-is — LightRAG must not re-chunk.
        try:
            _lightrag = LightRAG(
                working_dir=settings.lightrag_workspace,
                embedding_func=_make_embedding_func(),
                embedding_bindings=settings.embedding_binding,
                embedding_binding_name=settings.embedding_binding,
                embedding_dim=settings.embedding_dim,
                llm_model_func=_make_llm_func(),
                llm_model_name=settings.llm_model_extract,
                llm_model_kwargs={
                    "base_url": settings.llm_base_url,
                    "api_key": settings.llm_api_key,
                },
                storage="PostgresStorage",
                kv_storage="PGKVStorage",
                doc_status_storage="PGDocStatusStorage",
                graph_storage="PGTableGraphStorage",
                vector_storage="PGVectorStorage",
                addon_params={
                    "language": "Vietnamese",
                    "entity_type_prompt_file": "prompts/entity_type/legal_vn.yml",
                    "entity_extraction_use_json": True,
                },
                chunk_token_size=settings.chunk_cap,
                chunk_overlap_token_size=50,
                enable_llm_cache=True,
                max_async=settings.max_async_llm,
                max_parallel=settings.max_parallel_workers,
            )
        except Exception:
            # Retry with minimal kwargs in case 1.5.6 does not support the new params.
            logger.warning("LightRAG init lỗi với đủ kwargs — thử fallback tối thiểu", exc_info=True)
            try:
                _lightrag = LightRAG(
                    working_dir=settings.lightrag_workspace,
                    embedding_func=_make_embedding_func(),
                    embedding_dim=settings.embedding_dim,
                    llm_model_func=_make_llm_func(),
                    storage="PostgresStorage",
                    kv_storage="PGKVStorage",
                    doc_status_storage="PGDocStatusStorage",
                    graph_storage="PGTableGraphStorage",
                    vector_storage="PGVectorStorage",
                    addon_params={
                        "language": "Vietnamese",
                        "entity_type_prompt_file": "prompts/entity_type/legal_vn.yml",
                        "entity_extraction_use_json": True,
                    },
                )
            except Exception as exc:  # pragma: no cover
                raise LightRAGUnavailableError(f"LightRAG init fail cả 2 path: {exc}") from exc

        logger.info("LightRAG sẵn sàng (workspace=%s, binding=%s)", settings.lightrag_workspace, settings.embedding_binding)
        LIGHTRAG_READY = True
        return _lightrag


async def ainsert_document(rag: Any, doc_id: str, chunks: list[str], chunk_ids: list[str]) -> None:
    """Insert one document into LightRAG — ids/file_paths map 1:1 to registry chunk_ids.

    (plan §3.2 step 6) Insert after COMMIT, with chunking_func passthrough.
    """
    try:
        await rag.ainsert(
            chunks,
            ids=chunk_ids,
            file_paths=[doc_id] * len(chunks),
            chunking_func=lambda c: [c],  # passthrough (A1) — verify signature (spike)
        )
    except TypeError:
        # The 1.5.6 signature may lack chunking_func.
        logger.warning("LightRAG ainsert không nhận chunking_func — fallback không passthrough")
        await rag.ainsert(chunks, ids=chunk_ids, file_paths=[doc_id] * len(chunks))
    except Exception as exc:  # noqa: BLE001 — surface the error; callers decide on retry
        raise RuntimeError(f"ainsert LightRAG lỗi (doc={doc_id}): {exc}") from exc


async def adelete_by_doc_id(rag: Any, lightrag_doc_id: str) -> None:
    """Remove a document from LightRAG on doc invalidation (plan §3.6)."""
    try:
        await rag.adelete_by_doc_id(lightrag_doc_id)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"adelete LightRAG lỗi (id={lightrag_doc_id}): {exc}") from exc
