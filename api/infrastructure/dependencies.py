"""Dependency factories — cached singletons behind lazy proxies.

Factories read Settings on first use so the api package imports cleanly before
configuration is ready (parallel scaffolding / smoke imports). Call sites should
prefer get_llm() / get_reranker() from here over direct adapter construction.
"""

from __future__ import annotations

from typing import Any

from api.infrastructure.adapters.google_places import GooglePlaces
from api.infrastructure.adapters.http_rerank import HttpRerank
from api.infrastructure.adapters.lightrag import LightRag
from api.infrastructure.adapters.noop import NoopRerank
from api.infrastructure.adapters.openai_compatible_llm import OpenAICompatibleLLM
from api.infrastructure.adapters.postgres_sql import PostgresSql
from api.infrastructure.adapters.static_places import StaticPlaces
from api.infrastructure.config.config import get_settings
from api.domain.value_objects.constants import (
    DEFAULT_MODEL_ANSWER,
    DEFAULT_RERANK_MODEL,
    MODEL_ROLE_FIELD,
    RERANK_BINDINGS,
)
from api.infrastructure.ports.geo import GeoPort
from api.infrastructure.ports.llm import LLMChatPort
from api.infrastructure.ports.rag import RagPort
from api.infrastructure.ports.rerank import RerankPort
from api.infrastructure.ports.sql import SqlPort

_llm: OpenAICompatibleLLM | None = None
_reranker: RerankPort | None = None
_geo: GeoPort | None = None
_rag: RagPort | None = None
_sql: SqlPort | None = None


def get_llm() -> LLMChatPort:
    """Build (once) the chat adapter from Settings; raises LLMConfigError if unconfigured."""
    global _llm
    if _llm is None:
        s = get_settings()
        _llm = OpenAICompatibleLLM(
            api_key=s.llm_api_key or "",
            base_url=s.llm_base_url_v1 or "",
            default_model=s.llm_model_answer or DEFAULT_MODEL_ANSWER,
        )
    return _llm


def get_reranker() -> RerankPort:
    """Build (once) the rerank adapter — NoopRerank when disabled by config."""
    global _reranker
    if _reranker is None:
        s = get_settings()
        binding = (s.rerank_binding or "").strip().lower()
        if not s.enable_rerank or binding not in RERANK_BINDINGS:
            _reranker = NoopRerank()
        else:
            _reranker = HttpRerank(
                api_key=s.rerank_api_key or "",
                base_url=s.rerank_base_url or "",
                binding=binding,
                model=s.rerank_model or DEFAULT_RERANK_MODEL,
            )
    return _reranker


def model_for_role(role: str) -> str:
    """Default model for a role (e.g. 'rewrite' -> LLM_MODEL_REWRITE)."""
    s = get_settings()
    field = MODEL_ROLE_FIELD.get(role, "llm_model_answer")
    return getattr(s, field, None) or s.llm_model_answer or DEFAULT_MODEL_ANSWER


def get_geo() -> GeoPort:
    """Build (once) the geo adapter — GooglePlaces when configured, else StaticPlaces."""
    global _geo
    if _geo is None:
        s = get_settings()
        binding = (s.geo_binding or "").strip().lower()
        if binding == "google" and s.geo_api_key:
            _geo = GooglePlaces(
                api_key=s.geo_api_key,
                base_url=s.geo_base_url,
                radius_m=s.geo_radius_m,
            )
        else:
            _geo = StaticPlaces(path=s.geo_static_path, radius_m=s.geo_radius_m)
    return _geo


def get_rag() -> RagPort:
    """Build (once) the RAG adapter — lazy LightRAG singleton behind the port."""
    global _rag
    if _rag is None:
        _rag = LightRag()
    return _rag


def get_sql() -> SqlPort:
    """Build (once) the read-only SQL adapter (R1 spec + R2 NL2SQL)."""
    global _sql
    if _sql is None:
        _sql = PostgresSql()
    return _sql


class LazyLLMProxy:
    """Forwards attribute access to the real adapter, built on first use.

    Lets `from ...dependencies import llm` stay import-safe pre-config.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_llm(), name)


llm: LLMChatPort = LazyLLMProxy()  # type: ignore[assignment]

