"""Central app settings — the single source of truth for env config.

Loads .env then .env.<APP_ENV> override from the repo root by absolute path,
so behavior is independent of the process CWD; api, ingest, and eval import Settings from here.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("api.config")

# Emitted once per process even if Settings is instantiated repeatedly (tests);
# the fallback itself is per-instance by design.
_warned_ephemeral_anon_secret = False

# Effort levels the [OI]-compatible endpoint accepts for reasoning_effort
# (live-verified: "xhigh" is rejected on the Gemini route; the four below are
# the portable set). The adapter clamps "none" -> "low" for Gemini 3.x models,
# which cannot fully disable thinking.
_REASONING_EFFORTS = ("none", "low", "medium", "high")


def _validate_reasoning_effort(value: str) -> str:
    """Boundary check: reject env values the chat endpoint would 400 on."""
    normalized = value.strip().lower()
    if normalized not in _REASONING_EFFORTS:
        raise ValueError(
            f"LLM_REASONING_EFFORT must be one of {', '.join(_REASONING_EFFORTS)}"
        )
    return normalized


LlmReasoningEffort = Annotated[str, AfterValidator(_validate_reasoning_effort)]

_REPO_ROOT = (
    Path(__file__).resolve().parents[3]
)  # parents[3] = repo root (HF-0: stale parents[1] pointed at api/infrastructure, breaking .env load)  # noqa: E501
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

    # Placeholder / guessable ANON_IDENTITY_SECRET values that production must
    # refuse; the 32-char floor below catches short placeholders like "secret".
    _KNOWN_WEAK_ANON_SECRETS = (
        "__GENERATE_ME__",
        "generate-me",
        "generate_me",
        "changeme",
        "change-me",
        "change_me",
        "secret",
        "password",
        "anonymous",
        "anon_secret",
    )
    _KNOWN_WEAK_LEAD_MIRROR_SECRETS = _KNOWN_WEAK_ANON_SECRETS + (
        "rag-real-estate-lead-mirror-default-secret",
    )

    @model_validator(mode="after")
    def _enforce_approved_llm_provider(self) -> Settings:
        """Enforce the audited worker route only when explicitly enabled."""
        if not self.enforce_approved_llm_provider:
            return self
        forbidden = ("sol", "openrouter", "ox-alpha")
        if any(token in (self.llm_base_url or "").lower() for token in forbidden):
            raise ValueError("LLM_BASE_URL must point to provider-anh-vu")
        model_fields = (
            "llm_model_rewrite", "llm_model_extract", "llm_model_answer",
            "llm_model_answer_pro", "llm_model_guard", "llm_model_nl2sql",
        )
        if any(getattr(self, field) != "gpt-5.6-luna" for field in model_fields):
            raise ValueError("All LLM model roles must use gpt-5.6-luna")
        return self

    @model_validator(mode="after")
    def _fail_fast_on_default_secrets(self) -> Settings:
        if self.app_env in ("prod", "production"):
            if self.postgres_password in self._KNOWN_DEFAULT_SECRETS:
                raise ValueError(
                    "POSTGRES_PASSWORD must be set and distinct from the dev default in production"
                )
            if not self.llm_api_key:
                raise ValueError("LLM_API_KEY is required in production")
            lead_secret = self.lead_mirror_hmac_secret.strip()
            if (
                not lead_secret
                or lead_secret.lower() in self._KNOWN_WEAK_LEAD_MIRROR_SECRETS
                or len(lead_secret) < 32
            ):
                raise ValueError(
                    "LEAD_MIRROR_HMAC_SECRET must be a strong, non-guessable secret "
                    "(min 32 chars) in production"
                )
            anon_secret = self.anon_identity_secret
            if (
                not anon_secret
                or anon_secret in self._KNOWN_WEAK_ANON_SECRETS
                or len(anon_secret) < 32
            ):
                raise ValueError(
                    "ANON_IDENTITY_SECRET must be a strong, non-guessable secret "
                    "(min 32 chars) in production"
                )
            if self.sales_legacy_key_auth_enabled:
                raise ValueError("SALES_LEGACY_KEY_AUTH_ENABLED must be disabled in production")
        if self.sales_legacy_key_auth_enabled and self.app_env.strip().lower() not in {
            "dev",
            "development",
            "test",
        }:
            raise ValueError(
                "SALES_LEGACY_KEY_AUTH_ENABLED requires APP_ENV=dev, development, or test"
            )
        return self

    @model_validator(mode="after")
    def _fallback_to_ephemeral_anon_secret_outside_prod(self) -> Settings:
        # Live regression (secure wave G2): dev boots with an empty secret and
        # mint-time construction raised ValueError -> GET /api/anon/token 500ed,
        # which the FE surfaces as a dead chat. Dev/test instead generate an
        # ephemeral per-process secret so no endpoint can fail on missing config;
        # production keeps the fail-fast validator above.
        if self.app_env in ("prod", "production") or self.anon_identity_secret:
            return self
        object.__setattr__(self, "anon_identity_secret", secrets.token_urlsafe(48))
        global _warned_ephemeral_anon_secret
        if not _warned_ephemeral_anon_secret:
            _warned_ephemeral_anon_secret = True
            logger.warning(
                "ANON_IDENTITY_SECRET not set; generated an ephemeral signing "
                "secret for this process only (dev/test fallback). Anonymous "
                "identities reset on restart — append ANON_IDENTITY_SECRET to "
                ".env for persistence."
            )
        return self

    @model_validator(mode="after")
    def _enforce_cors_origins_safety(self) -> Settings:
        """CORS allowlist must be explicit and never a wildcard.

        Fail closed: production refuses to start when CORS_ORIGINS is missing
        or empty (the localhost default is a dev/test convenience that would
        silently lock a production widget out of its real origin), and every
        environment rejects the ``"*"`` wildcard because the API carries
        credentialed/auth endpoints. Dev/test without an explicit value fall
        back to an explicitly controlled safe localhost origin.
        """
        origins = self.cors_origins
        is_production = self.app_env.strip().lower() in ("prod", "production")
        if not origins:
            if is_production:
                raise ValueError(
                    "CORS_ORIGINS is required in production (wildcard not allowed)"
                )
            origins = ["http://localhost:3000"]
            object.__setattr__(self, "cors_origins", origins)
        if any(isinstance(origin, str) and origin.strip() == "*" for origin in origins):
            raise ValueError(
                "CORS_ORIGINS must not contain the '*' wildcard; the API has "
                "credentialed/auth endpoints"
            )
        return self

    # App
    app_env: str = "dev"
    # None (unset) -> dev/test resolve to the safe localhost default; production
    # fails fast via _enforce_cors_origins_safety. The "*" wildcard is rejected
    # in every environment (credentialed/auth endpoints exist).
    cors_origins: list[str] | None = None

    # Postgres (LightRAG reads POSTGRES_* directly)
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "ragre"
    postgres_password: str = "ragre_dev_password"
    postgres_database: str = "ragre"
    postgres_max_connections: int = 10

    # LightRAG storage
    lightrag_workspace: str = "ragre_mvp"
    # Workspace that holds Soleil's LightRAG rows (db/migrations/
    # 2026-08-28-soleil-lightrag-scope-repair.sql moved them there deliberately
    # for project isolation). The query path routes soleil reads here; every
    # other project keeps the historical default-workspace instance.
    lightrag_workspace_soleil: str = "ragre_mvp"

    @field_validator("lightrag_workspace_soleil")
    @classmethod
    def _validate_lightrag_workspace_soleil(cls, value: str) -> str:
        # Soleil must never resolve onto the shared default namespace --
        # absolute project isolation; fail closed at config load.
        # LightRAG ctor defaults workspace to "" (lightrag/lightrag.py:413);
        # PG storage maps empty/falsy to "default" (postgres_impl.py:2700-2702).
        # Both representations mean "shared default namespace" and would
        # collapse Soleil rows into the same namespace as untagged projects.
        normalized = value.strip()
        if not normalized:
            raise ValueError("LIGHTRAG_WORKSPACE_SOLEIL must not be empty or whitespace-only")
        if normalized.lower() == "default":
            raise ValueError(
                "LIGHTRAG_WORKSPACE_SOLEIL must not be 'default': "
                "that is the shared PG namespace (postgres_impl.py fallback) "
                "and would collapse Soleil into untagged projects"
            )
        return normalized

    # RAG pipeline warm-up: run the lazy LightRAG init (get_lightrag +
    # initialize_storages, ~30-40s cold) in a startup background task so the
    # first query does not pay the cold-start cost. Idempotent by construction;
    # failure degrades to the lazy first-query path without crashing startup.
    rag_prewarm_enabled: bool = True

    # Prompt assets — canonical api/prompts/ dir (HF-0). Exported as PROMPT_DIR so
    # LightRAG 1.5.6 resolves entity_type/<file> under it (bare filename contract).
    prompt_dir: str = str(_REPO_ROOT / "api" / "prompts")

    # Embedding (LOCK: gemini-embedding-001, dims 1024 — a change means a full re-embed)
    embedding_binding: str = "gemini"  # dashscope | aibox | local | openrouter | gemini
    embedding_api_key: str = ""
    embedding_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 1024
    # Revision-3 opt-in: OpenRouter-compatible embedding route. The binding above
    # stays "dashscope" by default; turning it to "openrouter" (with
    # OPENROUTER_API_KEY set from the secret store) activates the selected model
    # below at the locked 1024 dims with encoding_format=float.
    # Shared OpenRouter credential for the revision-3 outbound adapters.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    embedding_openrouter_model: str = "openai/text-embedding-3-small"
    # Hostname allowlist for outbound embedding/rerank HTTP calls (SSRF guard).
    # gemini host added for the Google [OI]-compat embedding route.
    outbound_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["openrouter.ai", "generativelanguage.googleapis.com"]
    )
    # Development-only opt-in: allow loopback/private outbound endpoints (tests / local dev)
    outbound_allow_private: bool = False

    # Rerank (app-side; single score source for confidence)
    rerank_binding: str = "dashscope"  # dashscope | aibox | openrouter | null
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = "qwen3-rerank"
    # Revision-3 rerank: voyageai/rerank-2.5 via the OpenRouter /api/v1/rerank route.
    rerank_openrouter_model: str = "voyageai/rerank-2.5"
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
    # JSON map of project_key to public CDN base URL. Empty uses the R2 base.
    image_cdn_project_map: str = "{}"

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

    def image_cdn_base(self, project_key: str) -> str:
        """Return one project's normalized public origin.

        ``IMAGE_CDN_PROJECT_MAP`` supports a legacy string value and the canonical
        object form (``{"origins": [...], "path_prefixes": [...]}``). Never
        stringify the object form into a Python-dict URL and never borrow another
        project's origin.
        """
        try:
            mapping = json.loads(self.image_cdn_project_map or "{}")
        except (TypeError, json.JSONDecodeError):
            mapping = {}
        has_mapping = isinstance(mapping, dict) and bool(mapping)
        value = mapping.get(project_key) if has_mapping else None
        if isinstance(value, dict):
            origins = value.get("origins", value.get("origin", []))
            origins = origins if isinstance(origins, list) else [origins]
            value = next((item for item in origins if isinstance(item, str) and item.strip()), None)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
        # An explicit map is an allowlist. Missing/invalid project entries must
        # not silently inherit the account-wide base or another project's host.
        return "" if has_mapping else self.r2_public_base

    def image_cdn_path_prefixes(self, project_key: str) -> tuple[str, ...]:
        """Return explicit canonical object-key prefixes for one project."""
        try:
            mapping = json.loads(self.image_cdn_project_map or "{}")
        except (TypeError, json.JSONDecodeError):
            mapping = {}
        value = mapping.get(project_key) if isinstance(mapping, dict) else None
        prefixes = value.get("path_prefixes", []) if isinstance(value, dict) else []
        return tuple(
            prefix.strip().lstrip("/")
            for prefix in prefixes
            if isinstance(prefix, str) and prefix.strip()
        )

    # LLM gateway (OpenAI-compatible)
    llm_api_key: str = ""
    llm_base_url: str = ""
    # MVP audit contract: every LLM call uses the approved provider/model only.
    llm_model_rewrite: str = "gpt-5.6-luna"
    llm_model_extract: str = "gpt-5.6-luna"
    llm_model_answer: str = "gpt-5.6-luna"
    llm_model_answer_pro: str = "gpt-5.6-luna"
    llm_model_guard: str = "gpt-5.6-luna"
    llm_model_nl2sql: str = "gpt-5.6-luna"
    # Enable the audited provider/model contract explicitly for production workers.
    enforce_approved_llm_provider: bool = False
    # Thinking-token control for the [OI]-compatible (Gemini) route: the endpoint
    # rejects native thinkingConfig but honors reasoning_effort. luna keeps its
    # pinned "xhigh" regardless; Gemini 3.x clamps "none" -> "low" in the adapter.
    llm_reasoning_effort: LlmReasoningEffort = "low"
    # Total budget (seconds) for one answer-generation LLM stream. Generous so a
    # long, table-heavy sales answer (up to 6000 tokens) is never cut mid-stream
    # while the token budget is still the primary bound (per-read timeout is 20s).
    llm_timeout_s: float = 120.0
    # Hard total deadline (seconds) for one whole SSE /query stream (wall clock
    # anchored at stream start; heartbeats never reset it). Bounds a pipeline
    # that hangs end-to-end (provider stall, unbounded post-pipeline await) and
    # ships a terminal error+done frame so the FE never spins indefinitely.
    sse_total_timeout_s: float = 150.0
    # Per-call budget (seconds) for the slot-fill LLM completion. The LLM legs
    # now run on a Google Gemini key via the OpenAI-compatible endpoint, whose
    # cold calls routinely exceed the old 6s hard-coded budget; raise the floor
    # and keep it env-tunable. conv_slots still fails open on a real timeout,
    # so a generous value only avoids spurious empty-slot degrade.
    llm_slot_fill_timeout_s: float = 30.0
    # Deadline (seconds) for ONE rag_leg aquery_data call. LightRAG hybrid
    # retrieval fans out to embedding + PG graph queries and a cold path can run
    # well past the old 15s default; the retry-once behavior is unchanged, only
    # the per-attempt budget is enlarged and made env-configurable.
    rag_aquery_timeout_s: float = 180.0

    # LightRAG query mode for the RAG leg. Validated against the modes LightRAG
    # 1.5.6's QueryParam accepts so a typo'd env value fails at settings load,
    # not on the first query.
    rag_query_mode: str = "hybrid"

    @field_validator("rag_query_mode")
    @classmethod
    def _validate_rag_query_mode(cls, value: str) -> str:
        allowed = ("naive", "local", "global", "hybrid", "mix")
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(f"RAG_QUERY_MODE must be one of {', '.join(allowed)}")
        return normalized

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
    firebase_vapid_key: str = ""
    # Legacy X-Sales-Key compatibility is opt-in and forbidden in production.
    sales_legacy_key_auth_enabled: bool = False
    # Optional preferred sales identity for lead routing (test/verification
    # harness). When set to the Firebase uid of an ACTIVE, mapped sales row,
    # every assignment selects that exact identity first so the account always
    # receives test leads for realtime manual verification. Blank preserves the
    # LRU-first / priority-tiebreak algorithm — production fairness is
    # unchanged unless this key is explicitly configured. Env-only; never
    # hardcode a uid in source or fixtures.
    sales_preferred_firebase_uid: str = ""

    # Lead mirror (story 9.2, hybrid D1). Production must provide this stable
    # HMAC key through the environment or a secret service; there is no usable
    # source default. Tests may inject an explicit fixture value.
    lead_mirror_hmac_secret: str = ""
    lead_mirror_reconciliation_enabled: bool = True
    lead_mirror_stale_after_minutes: int = 300
    lead_mirror_stale_batch_limit: int = 50
    lead_mirror_sweep_interval_seconds: int = 300
    # Explicit bounded deadlines and retry budget for the optional Firestore mirror.
    firestore_connect_timeout_seconds: float = 2.0
    firestore_read_timeout_seconds: float = 5.0
    firestore_write_timeout_seconds: float = 5.0
    firestore_retry_attempts: int = 2

    # Signed anonymous identity (secure wave G2/G4). Empty default is fine in
    # dev/test: the validator above substitutes an ephemeral per-process secret
    # so minting never 500s; production fails fast instead.
    anon_identity_secret: str = ""
    # Post-lead bonus policy (spec §7): one-time extra turns per identity.
    anonymous_bonus_turns_after_lead: int = 5
    # Per-IP secondary brakes (spec §9). Generous defaults absorb shared NAT
    # egress; tighten via env once real traffic patterns are known.
    ip_rate_limit_window_seconds: int = 3600
    ip_rate_limit_mint_max_requests: int = 30
    ip_rate_limit_query_max_requests: int = 120
    ip_rate_limit_lead_max_requests: int = 10
    # Reverse proxies allowed to append X-Forwarded-For (spec §9). XFF is only
    # honored when the immediate TCP peer is one of these IPs; any other client
    # is keyed by its direct peer address so the header can't spoof a different
    # per-IP allowance. Accepts a comma-separated env string or JSON list.
    trusted_proxy_ips: list[str] = Field(default_factory=list)
    chat_history_retention_days: int = 30
    lead_cta_after_turns: int = 3

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def _split_comma_separated_proxy_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

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
    per-project registry. The async answer path now reads the geo center from
    the per-request registry record (api/application/ports/project_registry.py)
    and never calls this helper; this synchronous psycopg2 seam is kept only
    for unbound direct callers (eval CLI, workflow unit tests) that still
    resolve the center outside a request. Best-effort and synchronous: any
    failure falls back to the configured defaults so the nearby-places leg
    never crashes.
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
