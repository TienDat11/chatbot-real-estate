"""Dependency factories — cached singletons behind lazy proxies.

Factories read Settings on first use so the api package imports cleanly before
configuration is ready (parallel scaffolding / smoke imports). Call sites should
prefer get_llm() / get_reranker() from here over direct adapter construction.
"""

from __future__ import annotations

from typing import Any

from api.application.ports.project_registry import ProjectRegistryPort
from api.domain.value_objects.constants import (
    DEFAULT_MODEL_ANSWER,
    DEFAULT_RERANK_MODEL,
    MODEL_ROLE_FIELD,
    RERANK_BINDINGS,
    RERANK_MODEL_OPENROUTER,
)
from api.infrastructure.adapters.google_places import GooglePlaces
from api.infrastructure.adapters.http_rerank import HttpRerank
from api.infrastructure.adapters.lightrag import LightRag
from api.infrastructure.adapters.noop import NoopRerank
from api.infrastructure.adapters.openai_compatible_llm import OpenAICompatibleLLM
from api.infrastructure.adapters.postgres_sql import PostgresSql
from api.infrastructure.adapters.static_places import StaticPlaces
from api.infrastructure.config.config import get_settings
from api.infrastructure.ports.firebase_auth import FirebaseAuthTokenVerifier
from api.infrastructure.ports.geo import GeoPort
from api.infrastructure.ports.llm import LLMChatPort
from api.infrastructure.ports.rag import RagPort
from api.infrastructure.ports.realtime_mirror import RealtimeLeadMirror
from api.infrastructure.ports.rerank import RerankPort
from api.infrastructure.ports.sql import SqlPort

_llm: OpenAICompatibleLLM | None = None
_reranker: RerankPort | None = None
_geo: GeoPort | None = None
_rag: RagPort | None = None
_sql: SqlPort | None = None
_firebase_auth_verifier: FirebaseAuthTokenVerifier | None = None
_realtime_lead_mirror: RealtimeLeadMirror | None = None
_project_registry: ProjectRegistryPort | None = None
_need_profile_embedding: Any | None = None
_reengage_queue_store: Any | None = None
_staff_audit_store: Any | None = None
_sales_provisioner: Any | None = None
_sales_notifications_store: Any | None = None
_fcm_tokens: Any | None = None
_fcm_sender: Any | None = None
_chat_history_repository: Any | None = None


def get_llm() -> LLMChatPort:
    """Build (once) the chat adapter from Settings; raises LLMConfigError if unconfigured."""
    global _llm
    if _llm is None:
        s = get_settings()
        _llm = OpenAICompatibleLLM(
            api_key=s.llm_api_key or "",
            base_url=s.llm_base_url_v1 or "",
            default_model=s.llm_model_answer or DEFAULT_MODEL_ANSWER,
            enforce_approved_provider=s.enforce_approved_llm_provider,
            reasoning_effort=s.llm_reasoning_effort,
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
            if binding == "openrouter":
                api_key = s.openrouter_api_key or s.rerank_api_key or ""
                base_url = s.openrouter_base_url or s.rerank_base_url or ""
                model = s.rerank_openrouter_model or RERANK_MODEL_OPENROUTER
            else:
                api_key = s.rerank_api_key or ""
                base_url = s.rerank_base_url or ""
                model = s.rerank_model or DEFAULT_RERANK_MODEL

            _reranker = HttpRerank(
                api_key=api_key,
                base_url=base_url,
                binding=binding,
                model=model,
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


def get_firebase_auth_verifier() -> FirebaseAuthTokenVerifier:
    """Build (once) the JWKS verifier — always live, independent of the mirror binding.

    Auth must not depend on the realtime binding: a future transport swap
    replaces the mirror adapter only, never the token verifier.
    """
    global _firebase_auth_verifier
    if _firebase_auth_verifier is None:
        from api.infrastructure.adapters.firebase_auth_jwks import FirebaseAuthJwksVerifier

        s = get_settings()
        _firebase_auth_verifier = FirebaseAuthJwksVerifier(
            project_id=s.firebase_project_id,
            jwks_url=s.firebase_jwks_url,
            issuer=s.firebase_auth_issuer,
            audience=s.firebase_project_id,
        )
    return _firebase_auth_verifier


def get_project_registry() -> ProjectRegistryPort:
    """Build (once) the async Postgres registry adapter (B2/M1 single read path)."""
    global _project_registry
    if _project_registry is None:
        from api.infrastructure.adapters.postgres_project_registry import (
            PostgresProjectRegistry,
        )

        _project_registry = PostgresProjectRegistry()
    return _project_registry


async def get_realtime_lead_mirror() -> RealtimeLeadMirror:
    """Build (once) the lead mirror — Noop when the firebase binding is off."""
    global _realtime_lead_mirror
    if _realtime_lead_mirror is None:
        from api.infrastructure.adapters.noop_realtime_mirror import NoopRealtimeLeadMirror

        s = get_settings()
        binding = (s.firebase_binding or "").strip().lower()
        if binding == "firestore":
            from api.infrastructure.adapters.firestore_rest_mirror import FirestoreRestLeadMirror

            _realtime_lead_mirror = FirestoreRestLeadMirror(
                project_id=s.firebase_project_id,
                service_account_client_email=s.firebase_service_account_client_email,
                service_account_private_key=s.firebase_service_account_private_key,
                rest_base_url=s.firebase_firestore_rest_base_url,
            )
        else:
            _realtime_lead_mirror = NoopRealtimeLeadMirror()
    return _realtime_lead_mirror


async def get_need_profile_embedding():
    """Build (once) the need-profile embedder for the re-approach pipeline.

    Any OpenAI-compatible binding works; missing credentials fail at wiring
    time so the admin trigger maps it to a clean 503 instead of a mid-run 401.
    """
    global _need_profile_embedding
    if _need_profile_embedding is None:
        from api.infrastructure.adapters.openai_compatible_embedding import (
            OpenAICompatibleNeedProfileEmbedding,
        )

        _need_profile_embedding = OpenAICompatibleNeedProfileEmbedding()
    return _need_profile_embedding


async def get_reengage_queue_store():
    """Build (once) the re-approach queue store — Noop when firebase is off."""
    global _reengage_queue_store
    if _reengage_queue_store is None:
        from api.infrastructure.adapters.noop_reengage_queue_store import (
            NoopReengageQueueStore,
        )

        s = get_settings()
        binding = (s.firebase_binding or "").strip().lower()
        if binding == "firestore":
            from api.infrastructure.adapters.firestore_reengage_queue_store import (
                FirestoreReengageQueueStore,
            )

            _reengage_queue_store = FirestoreReengageQueueStore(
                project_id=s.firebase_project_id,
                service_account_client_email=s.firebase_service_account_client_email,
                service_account_private_key=s.firebase_service_account_private_key,
                rest_base_url=s.firebase_firestore_rest_base_url,
            )
        else:
            _reengage_queue_store = NoopReengageQueueStore()
    return _reengage_queue_store


def get_staff_audit_store():
    """Build (once) the durable staff audit store (story 9.5, PG-backed)."""
    global _staff_audit_store
    if _staff_audit_store is None:
        from api.infrastructure.adapters.postgres_staff_audit import (
            PostgresStaffAuditStore,
        )

        _staff_audit_store = PostgresStaffAuditStore()
    return _staff_audit_store


def get_chat_history_repository():
    """Build the durable chat-history repository for staff transcript reads."""
    global _chat_history_repository
    if _chat_history_repository is None:
        from api.infrastructure.adapters.postgres_chat_history import repository

        _chat_history_repository = repository
    return _chat_history_repository


def get_sales_notifications_store():
    """Build (once) the PG sales-notification read-model adapter (G3-r6).

    The adapter implements both the notifications repository and the narrow
    assigned-lead seam the CRM transcript route re-checks; the DDL itself is
    owned by the 2026-08-28 migration (no runtime CREATE TABLE fallback).
    """
    global _sales_notifications_store
    if _sales_notifications_store is None:
        from api.infrastructure.adapters.postgres_sales_notifications import (
            PostgresSalesNotificationsRepository,
        )

        _sales_notifications_store = PostgresSalesNotificationsRepository()
    return _sales_notifications_store


def get_fcm_tokens():
    global _fcm_tokens
    if _fcm_tokens is None:
        from api.infrastructure.adapters.postgres_fcm_tokens import PostgresFcmTokenRepository
        _fcm_tokens = PostgresFcmTokenRepository()
    return _fcm_tokens


def get_fcm_sender():
    global _fcm_sender
    if _fcm_sender is None:
        from api.infrastructure.adapters.firebase_fcm import FirebaseFcmSender
        s = get_settings()
        _fcm_sender = FirebaseFcmSender(
            project_id=s.firebase_project_id,
            client_email=s.firebase_service_account_client_email,
            private_key=s.firebase_service_account_private_key,
        )
    return _fcm_sender


def get_fcm_notification_service():
    from api.application.services.fcm_notification_service import FcmNotificationService
    return FcmNotificationService(get_fcm_tokens(), get_fcm_sender())


def get_sales_provisioner():
    """Build the Firebase REST sales provisioner lazily."""
    global _sales_provisioner
    if _sales_provisioner is None:
        from api.infrastructure.adapters.firebase_sales_provisioner import FirebaseSalesProvisioner

        s = get_settings()
        _sales_provisioner = FirebaseSalesProvisioner(
            project_id=s.firebase_project_id,
            client_email=s.firebase_service_account_client_email,
            private_key=s.firebase_service_account_private_key,
            rest_base_url=s.firebase_firestore_rest_base_url,
        )
    return _sales_provisioner


class LazyLLMProxy:
    """Forwards attribute access to the real adapter, built on first use.

    Lets `from ...dependencies import llm` stay import-safe pre-config.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_llm(), name)


llm: LLMChatPort = LazyLLMProxy()  # type: ignore[assignment]
