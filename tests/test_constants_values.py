"""Unit tests for shared numeric constants: LLM timeouts + embedding dims lock.

The embedding dimensionality is a hard lock: a change forces a full corpus
re-embed, so the constant is cross-checked against the live Settings instance
(the second source of the same value) to catch drift between the two.
"""

from __future__ import annotations

from api.domain.value_objects.constants import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM_TIMEOUT_S,
    DEFAULT_RERANK_TIMEOUT_S,
    LLM_CALL_TIMEOUT_S,
)


def test_default_llm_timeout_is_20s():
    assert DEFAULT_LLM_TIMEOUT_S == 20.0


def test_llm_call_timeout_is_12s():
    # 12s keeps >=1.3x headroom over the observed <=9.3s rewrite/nl2sql calls.
    assert LLM_CALL_TIMEOUT_S == 12.0


def test_rerank_timeout_is_3s():
    assert DEFAULT_RERANK_TIMEOUT_S == 3.0


def test_embedding_dims_locked_at_1024():
    assert DEFAULT_EMBEDDING_DIM == 1024
    assert DEFAULT_EMBEDDING_MODEL == "text-embedding-v4"


def test_embedding_dims_match_settings():
    # The lock lives in two places; they must never disagree.
    from api.infrastructure.config.config import settings

    assert settings.embedding_dim == DEFAULT_EMBEDDING_DIM
    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
