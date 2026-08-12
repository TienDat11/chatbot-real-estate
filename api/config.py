"""Central application settings — the single source of truth for env config.

Loads `.env` (base) then `.env.<APP_ENV>` (override) from the repo root, using
absolute paths so behavior is independent of the process CWD. Every module
(api, ingest, eval) imports Settings from here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_ENV = os.getenv("APP_ENV", "dev")


class Settings(BaseSettings):
    """Application configuration; field name maps to env var (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), str(_REPO_ROOT / f".env.{_APP_ENV}")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_env: str = "dev"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- Postgres (LightRAG reads POSTGRES_* directly) ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "ragre"
    postgres_password: str = "ragre_dev_password"
    postgres_database: str = "ragre"
    postgres_max_connections: int = 10

    # --- LightRAG storage ---
    lightrag_workspace: str = "ragre_mvp"

    # --- Embedding (LOCK: text-embedding-v4, dims 1024 — change = full re-embed) ---
    embedding_binding: str = "dashscope"  # dashscope | aibox | local
    embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024

    # --- Rerank (app-side, single score source for confidence) ---
    rerank_binding: str = "dashscope"  # dashscope | aibox | null
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = "qwen3-rerank"
    enable_rerank: bool = True

    # --- Geo (nearby places — THE CAMELLIA project area, brief §7) ---
    geo_binding: str = "static"  # static | google | off
    geo_api_key: str = ""
    geo_base_url: str = "https://maps.googleapis.com/maps/api/place"
    geo_radius_m: int = 10000
    geo_static_path: str = "db/seed/static_places.json"
    geo_center_lat: float = 16.0558  # giao lộ Lê Văn Lương – Lê Đức Thọ, Sơn Trà (approx)
    geo_center_lng: float = 108.2455

    # --- LLM gateway (OpenAI-compatible) ---
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model_rewrite: str = "deepseek-v4-flash"
    llm_model_extract: str = "qwen3.7-flash"
    llm_model_answer: str = "deepseek-v4-flash"
    llm_model_answer_pro: str = "deepseek-v4-pro"
    llm_model_guard: str = "deepseek-v4-flash-0731"
    llm_model_nl2sql: str = "qwen3.7-flash"

    # --- Query token budgets (RAG leg) ---
    rag_max_entity_tokens: int = Field(default=2000, validation_alias="QUERY_MAX_ENTITY_TOKENS")
    rag_max_relation_tokens: int = Field(default=2000, validation_alias="QUERY_MAX_RELATION_TOKENS")
    rag_max_total_tokens: int = Field(default=6000, validation_alias="QUERY_MAX_TOTAL_TOKENS")

    # --- Guard ---
    guard_input_pg2_url: str | None = None  # optional Prompt Guard 2 endpoint

    # --- Ingest ---
    chunk_cap: int = 1200  # hard cap per chunk (A1)
    extract_timeout: float = 90.0  # seconds per extraction call
    max_async_llm: int = 6
    max_parallel_workers: int = 2

    # --- DSNs ---
    @property
    def pg_dsn(self) -> str:
        """asyncpg DSN for most queries (asyncpg driver)."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def pg_dsn_sync(self) -> str:
        """psycopg2 DSN (sync) — same information, different driver."""
        return self.pg_dsn

    @property
    def pg_dsn_ro(self) -> str:
        """Query-mode DSN; code runs SET LOCAL ROLE ro_query in-transaction for RLS."""
        return self.pg_dsn

    @property
    def query_max_entity_tokens(self) -> int:
        """Back-compat alias for legacy env names."""
        return self.rag_max_entity_tokens

    @property
    def query_max_relation_tokens(self) -> int:
        return self.rag_max_relation_tokens

    @property
    def query_max_total_tokens(self) -> int:
        return self.rag_max_total_tokens


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def export_runtime_env(cfg: Settings | None = None) -> None:
    """Export Settings-backed values to os.environ for env-reading libraries.

    LightRAG PG storages (PGKVStorage / PGTableGraphStorage / PGVectorStorage)
    read POSTGRES_* from the process environment, not from pydantic Settings —
    without this export they silently fall back to LightRAG defaults. setdefault
    keeps a real shell env authoritative over .env values.
    """
    resolved = cfg or get_settings()
    for key, value in {
        "POSTGRES_HOST": resolved.postgres_host,
        "POSTGRES_PORT": str(resolved.postgres_port),
        "POSTGRES_USER": resolved.postgres_user,
        "POSTGRES_PASSWORD": resolved.postgres_password,
        "POSTGRES_DATABASE": resolved.postgres_database,
        "POSTGRES_MAX_CONNECTIONS": str(resolved.postgres_max_connections),
    }.items():
        os.environ.setdefault(key, value)


settings = get_settings()
