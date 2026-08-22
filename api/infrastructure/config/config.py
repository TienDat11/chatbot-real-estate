"""Central app settings — the single source of truth for env config.

Loads .env then .env.<APP_ENV> override from the repo root by absolute path,
so behavior is independent of the process CWD; api, ingest, and eval import Settings from here.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("api.config")

_REPO_ROOT = Path(__file__).resolve().parents[3]  # parents[3] = repo root (HF-0: stale parents[1] pointed at api/infrastructure, breaking .env load)
_APP_ENV = os.getenv("APP_ENV", "dev")


class Settings(BaseSettings):
    """Application configuration; field name maps to env var (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), str(_REPO_ROOT / f".env.{_APP_ENV}")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Known development-only value; production startup fails fast when it survives.
    _KNOWN_DEFAULT_SECRETS = ("ragre_dev_password", "")

    @model_validator(mode="after")
    def _fail_fast_on_default_secrets(self) -> Settings:
        if self.app_env in ("prod", "production"):
            if self.postgres_password in self._KNOWN_DEFAULT_SECRETS:
                raise ValueError(
                    "POSTGRES_PASSWORD must be set and distinct from the dev default in production"
                )
            if not self.llm_api_key:
                raise ValueError("LLM_API_KEY is required in production")
        return self

    # App
    app_env: str = "dev"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Postgres (LightRAG reads POSTGRES_* directly)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "ragre"
    postgres_password: str = "ragre_dev_password"
    postgres_database: str = "ragre"
    postgres_max_connections: int = 10

    # LightRAG storage
    lightrag_workspace: str = "ragre_mvp"

    # Prompt assets — canonical api/prompts/ dir (HF-0). Exported as PROMPT_DIR so
    # LightRAG 1.5.6 resolves entity_type/<file> under it (bare filename contract).
    prompt_dir: str = str(_REPO_ROOT / "api" / "prompts")

    # Embedding (LOCK: text-embedding-v4, dims 1024 — a change means a full re-embed)
    embedding_binding: str = "dashscope"  # dashscope | aibox | local
    embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024

    # Rerank (app-side; single score source for confidence)
    rerank_binding: str = "dashscope"  # dashscope | aibox | null
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = "qwen3-rerank"
    enable_rerank: bool = True

    # Geo (nearby places). The Camellia lat/lng here are the LEGACY fallback used
    # when no project is bound or the project_config registry read fails
    # (story 8.2/10.2 — project_geo_center() is authoritative for scoped paths).
    geo_binding: str = "static"  # static | google | off
    geo_api_key: str = ""
    geo_base_url: str = "https://maps.googleapis.com/maps/api/place"
    geo_radius_m: int = 10000
    geo_static_path: str = "db/seed/static_places.json"
    geo_center_lat: float = 16.1052  # legacy fallback: The Camellia
    geo_center_lng: float = 108.2558  # legacy fallback: The Camellia

    # Cloudflare R2 object storage (image upload; credentials only, never hardcoded)
    r2_account_id: str = ""
    r2_endpoint: str = ""
    r2_bucket_name: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    # Optional public custom domain; empty falls back to the R2 public r2.dev host.
    r2_public_url: str = ""

    @property
    def r2_public_base(self) -> str:
        """Public base host for R2 objects.

        A custom domain is the explicit choice; otherwise derive the r2.dev
        public URL from the account id so callers never hardcode the host.
        """
        explicit = self.r2_public_url.strip()
        if explicit:
            return explicit.rstrip("/")
        return f"https://pub-{self.r2_account_id}.r2.dev"

    # LLM gateway (OpenAI-compatible)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model_rewrite: str = "deepseek-v4-flash"
    llm_model_extract: str = "qwen3.7-flash"
    llm_model_answer: str = "deepseek-v4-flash"
    llm_model_answer_pro: str = "deepseek-v4-pro-0813"
    llm_model_guard: str = "deepseek-v4-flash-0731"
    llm_model_nl2sql: str = "qwen3.7-flash"

    @property
    def llm_base_url_v1(self) -> str:
        """OpenAI-compatible base URL carrying the /v1 API path, whatever config holds.

        aibox / Qwen compatible gateways serve /chat/completions under /v1; the
        openai SDK 2.x turns a bare host into a plain-text response (no `.choices`),
        so all clients must pass the /v1 form. Normalize once here instead of at
        every call site, so both `.../v1` and bare-host config strings work.
        """
        base = (self.llm_base_url or "").strip()
        return base if base.rstrip("/").endswith("/v1") else base.rstrip("/") + "/v1"

    # Query token budgets (RAG leg)
    rag_max_entity_tokens: int = Field(default=2000, validation_alias="QUERY_MAX_ENTITY_TOKENS")
    rag_max_relation_tokens: int = Field(default=2000, validation_alias="QUERY_MAX_RELATION_TOKENS")
    rag_max_total_tokens: int = Field(default=6000, validation_alias="QUERY_MAX_TOTAL_TOKENS")

    # Guard
    guard_input_pg2_url: str | None = None  # optional Prompt Guard 2 endpoint

    # Ingest
    chunk_cap: int = 1200  # hard cap per chunk (A1)
    extract_timeout: float = 90.0  # seconds per extraction call
    max_async_llm: int = 6
    max_parallel_workers: int = 2

    # Firebase realtime layer (hybrid D1). The binding is the single switch for
    # the whole BE realtime layer; "firestore" activates the REST mirror and the
    # JWKS token verifier stays live regardless because auth must not depend on
    # the mirror binding. firebase-admin is banned by the stack lock, hence the
    # service-account fields below feed a pure httpx+PyJWT OAuth2 grant.
    firebase_binding: str = "off"  # off | firestore
    firebase_project_id: str = "sale-chat-bot-11e49"
    firebase_service_account_client_email: str = ""
    # Env-provided PEM with \n escapes (the JSON key-file form); decoded before signing.
    firebase_service_account_private_key: str = ""
    # Ops-only web key (session-cookie exchange later); never used for token verify.
    firebase_web_api_key: str = ""

    @property
    def firebase_firestore_rest_base_url(self) -> str:
        """Firestore REST v1 root — the no-SDK write path for the mirror."""
        return "https://firestore.googleapis.com/v1"

    @property
    def firebase_jwks_url(self) -> str:
        """Google's public JWKS for securetoken RS256 key rotation."""
        return "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"

    @property
    def firebase_auth_issuer(self) -> str:
        """Expected ID-token issuer for this Firebase project."""
        return f"https://securetoken.google.com/{self.firebase_project_id}"

    # DSNs
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

    LightRAG PG storages read POSTGRES_* from the process environment, not from
    Settings; setdefault keeps a real shell env authoritative over .env values.
    """
    resolved = cfg or get_settings()
    for key, value in {
        "POSTGRES_HOST": resolved.postgres_host,
        "POSTGRES_PORT": str(resolved.postgres_port),
        "POSTGRES_USER": resolved.postgres_user,
        "POSTGRES_PASSWORD": resolved.postgres_password,
        "POSTGRES_DATABASE": resolved.postgres_database,
        "POSTGRES_MAX_CONNECTIONS": str(resolved.postgres_max_connections),
        "PROMPT_DIR": resolved.prompt_dir,
    }.items():
        os.environ.setdefault(key, value)


def project_geo_center(project_key: str) -> tuple[float, float]:
    """Return the (lat, lng) geo center for a project from project_config.

    Story 8.2: the geo center moved from a hardcoded Camellia constant to the
    per-project registry. Callers that do not know the project yet keep the
    Settings Camellia defaults via get_cfg; this helper is for the project-
    scoped paths (ISSUE-03/05). Best-effort and synchronous: any failure falls
    back to the configured defaults so the nearby-places leg never crashes.
    """
    settings = get_settings()
    try:
        import psycopg2
    except ImportError:
        logger.warning("psycopg2 unavailable; using default geo center")
        return settings.geo_center_lat, settings.geo_center_lng
    try:
        with psycopg2.connect(settings.pg_dsn_sync, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT geo_center_lat, geo_center_lng FROM project_config "
                    "WHERE project_key = %s AND status = 'active'",
                    (project_key,),
                )
                row = cur.fetchone()
        if row and row[0] is not None and row[1] is not None:
            return float(row[0]), float(row[1])
    except Exception as exc:  # noqa: BLE001 — config read is best-effort
        logger.warning("project_config geo read failed (%s); using default center", exc)
    return settings.geo_center_lat, settings.geo_center_lng


settings = get_settings()
