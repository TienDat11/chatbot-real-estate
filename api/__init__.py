# -*- coding: utf-8 -*-
"""api package - 8-step query pipeline (guard, rewrite, legs, merge, generate, output guard).

Modules are plain functions / testable classes; workflow.py orchestrates them.

This is the public API surface. Internal structure follows DDD:
- domain/ - pure business logic (entities, value objects, domain services)
- application/ - use cases and orchestration (services, pipelines)
- infrastructure/ - external adapters (config, database, ports & adapters)
- interfaces/ - API entry points (FastAPI)
"""

# Backward-compat: get_cfg wrapper for settings access
def get_cfg(key: str, default=None):
    """Return a settings value by key (backward-compat wrapper around get_settings)."""
    from .infrastructure.config.config import get_settings
    cfg = get_settings()
    return getattr(cfg, key, default)

# Domain exports
from .domain.entities.price_calc import (
    parse_vn_number,
    extract_budget,
    extract_price_intent,
    floor_price_vnd,
    Offer,
    resolve_unit_type_key,
    cash_match,
    loan_match,
    affordability_rows,
    affordability_summary,
    analyze_affordability,
    offer_from_row,
)
from .domain.value_objects.constants import (
    DEFAULT_LLM_TIMEOUT_S,
    DEFAULT_RERANK_TIMEOUT_S,
    LLM_CALL_TIMEOUT_S,
    MAX_QUERY_LENGTH,
    MAX_INPUT_CHARS,
    SSE_EVENT_PLACES,
    SSE_EVENT_SOURCES,
    SSE_EVENT_FACTS,
    SSE_EVENT_TOKEN,
    SSE_EVENT_DONE,
    SSE_EVENT_ERROR,
    DEFAULT_MODEL_ANSWER,
    DEFAULT_MODEL_ANSWER_PRO,
    DEFAULT_MODEL_EXTRACT,
    DEFAULT_MODEL_GUARD,
    DEFAULT_MODEL_NL2SQL,
    DEFAULT_MODEL_REWRITE,
    MODEL_ROLE_FIELD,
    SUPPORTED_ROLES,
    RERANK_BINDINGS,
    DEFAULT_RERANK_MODEL,
    RERANK_ENDPOINT_DASHSCOPE,
    RERANK_ENDPOINT_AIBOX,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_MAX_ENTITY_TOKENS,
    DEFAULT_MAX_RELATION_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
)
from .domain.services.route_intent import Intent, ClassifyResult, classify_intent
from .domain.services.rewrite import (
    RoutedResult,
    fallback_route,
    rewrite_query,
    detect_aggregate_intent,
    HIGH_STAKES_KEYWORDS,
    AGGREGATE_KEYWORDS,
    GEO_INTENT_KEYWORDS,
    _normalize_routed,
)
from .domain.services.guard_input import GuardResult as InputGuardResult, guard_input, rule_screen
from .domain.services.guard_output import GuardResult as OutputGuardResult, guard_output
from .domain.services.rerank import rerank
from .domain.services.llm import (
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMTimeoutError,
    LazyLLMProxy,
    MODEL_ROLE_FIELD,
    OpenAICompatibleLLM,
    get_llm,
    llm,
    model_for_role,
)
from .domain.services.nl2sql_guard import (
    Sqlnl2sqlError,
    validate_sql,
    extract_sql,
    run_nl2sql,
    close_nl2sql_pool,
)
from .domain.services.utils import (
    sha256_hex,
    utc_now_iso,
    safe_float,
    truncate_str,
    slugify,
)

# Application exports
from .application.services.sql_leg import (
    SqlLegResult,
    SpecError,
    SqlLegError,
    build_dsn,
    get_ro_pool,
    close_ro_pool,
    run_sql_leg,
    ALLOWED_SOURCES,
    ALLOWED_FIELDS,
    ALLOWED_OPS,
    OFFER_COLUMNS,
)
from .application.services.rag_leg import (
    RagLegResult,
    run_rag_leg,
    LIGHTRAG_READY,
)
from .application.services.merge import (
    Merged,
    merge_context,
    build_rag_context,
    build_evidence_context,
    build_sources,
    build_facts,
    hydrate_chunks,
)
from .application.services.generate import (
    stream_answer,
    build_messages,
)
from .application.services.audit import (
    write_audit,
    close_audit_pool,
    redact_sql_spec,
    redact_sql_query,
)
from .application.pipelines.workflow import (
    RagQueryWorkflow,
    RagQueryPipeline,
    QueryRejected,
    STEP_TIMEOUTS,
    GuardedEv,
    RagRequestEv,
    SqlRequestEv,
    GeoRequestEv,
    RagDoneEv,
    SqlDoneEv,
    GeoDoneEv,
    MergedEv,
    GeneratedEv,
    parse_as_of,
)
from .application.pipelines.conv_workflow import (
    RagRgreConvWorkflow,
    RagQueryPipelineConv,
    SSE_EVENT_ROUTING,
)
from .application.services.conv_state import (
    ConvContext,
    get_context,
    mark_phone_given,
    transition,
    maybe_lead_cta_hint,
    note_useful_turn,
    conv_directive,
    register_interest,
    CTA_VARIANTS,
)
from .domain.services.conv_slots import (
    extract_bedrooms,
    extract_view,
    extract_timeline,
    extract_purpose,
    extract_slots_deterministic,
    extract_slots,
    lead_prefill_note,
)

# Infrastructure exports
from .infrastructure.config.config import (
    Settings,
    get_settings,
    export_runtime_env,
)
from .infrastructure.dependencies import (
    get_llm,
    get_reranker,
    get_geo,
    get_rag,
    get_sql,
    model_for_role,
)
from .infrastructure.ports import (
    GeoPlace,
    GeoPort,
    GeoResult,
    LLMChatPort,
    RagChunk,
    RagPort,
    RagResult,
    RerankPort,
    SqlPort,
    SqlResult,
)
from .infrastructure.adapters import (
    GooglePlaces,
    HttpRerank,
    LightRag,
    NoopRerank,
    LLMConfigError,
    LLMError,
    LLMTimeoutError,
    OpenAICompatibleLLM,
    PostgresSql,
    StaticPlaces,
)

# Interface exports
from .interfaces.api.main import create_app, app


# Backward-compat aliases for tests
import sys as _sys
_api_mod = _sys.modules[__name__]

# api.utils
_utils_mod = __import__(f"{__name__}.domain.services.utils", fromlist=["*"])
_sys.modules[__name__ + ".utils"] = _utils_mod

# api.rewrite
_rewrite_mod = __import__(f"{__name__}.domain.services.rewrite", fromlist=["*"])
_sys.modules[__name__ + ".rewrite"] = _rewrite_mod

# api.route_intent
_route_intent_mod = __import__(f"{__name__}.domain.services.route_intent", fromlist=["*"])
_sys.modules[__name__ + ".route_intent"] = _route_intent_mod

# api.guard_input
_guard_input_mod = __import__(f"{__name__}.domain.services.guard_input", fromlist=["*"])
_sys.modules[__name__ + ".guard_input"] = _guard_input_mod

# api.price_calc
_price_calc_mod = __import__(f"{__name__}.domain.entities.price_calc", fromlist=["*"])
_sys.modules[__name__ + ".price_calc"] = _price_calc_mod

# api.constants
_constants_mod = __import__(f"{__name__}.domain.value_objects.constants", fromlist=["*"])
_sys.modules[__name__ + ".constants"] = _constants_mod

# api.llm
_llm_mod = __import__(f"{__name__}.domain.services.llm", fromlist=["*"])
_sys.modules[__name__ + ".llm"] = _llm_mod

# api.sql_leg
_sql_leg_mod = __import__(f"{__name__}.application.services.sql_leg", fromlist=["*"])
_sys.modules[__name__ + ".sql_leg"] = _sql_leg_mod

# api.workflow
_workflow_mod = __import__(f"{__name__}.application.pipelines.workflow", fromlist=["*"])
_sys.modules[__name__ + ".workflow"] = _workflow_mod

# api.nl2sql_guard
_nl2sql_guard_mod = __import__(f"{__name__}.domain.services.nl2sql_guard", fromlist=["*"])
_sys.modules[__name__ + ".nl2sql_guard"] = _nl2sql_guard_mod
# Also set as attribute on api package for monkeypatch tests
_api_mod.nl2sql_guard = _nl2sql_guard_mod
# Also expose as api.nl2sql_guard.nl2sql_guard for monkeypatch tests
_sys.modules[__name__ + ".nl2sql_guard.nl2sql_guard"] = _nl2sql_guard_mod

# api.dependencies
_deps_mod = __import__(f"{__name__}.infrastructure.dependencies", fromlist=["*"])
_sys.modules[__name__ + ".dependencies"] = _deps_mod

# api.config
_config_mod = __import__(f"{__name__}.infrastructure.config.config", fromlist=["*"])
_sys.modules[__name__ + ".config"] = _config_mod

# api.adapters
_adapters_pkg = __import__(f"{__name__}.infrastructure.adapters", fromlist=["*"])
_sys.modules[__name__ + ".adapters"] = _adapters_pkg
# Also set as attribute on api package for monkeypatch tests
_api_mod.adapters = _adapters_pkg
# Register submodule aliases
for _name in ["google_places", "http_rerank", "lightrag", "noop", "openai_compatible_llm", "postgres_sql", "static_places"]:
    try:
        _submod = __import__(f"{__name__}.infrastructure.adapters.{_name}", fromlist=["*"])
        _sys.modules[__name__ + ".adapters." + _name] = _submod
        setattr(_adapters_pkg, _name, _submod)
    except ImportError:
        pass

# api.ports
_ports_mod = __import__(f"{__name__}.infrastructure.ports", fromlist=["*"])
_sys.modules[__name__ + ".ports"] = _ports_mod
# Register port submodules
_ports_geo = __import__(f"{__name__}.infrastructure.ports.geo", fromlist=["*"])
_sys.modules[__name__ + ".ports.geo"] = _ports_geo
_ports_llm = __import__(f"{__name__}.infrastructure.ports.llm", fromlist=["*"])
_sys.modules[__name__ + ".ports.llm"] = _ports_llm
_ports_rag = __import__(f"{__name__}.infrastructure.ports.rag", fromlist=["*"])
_sys.modules[__name__ + ".ports.rag"] = _ports_rag
_ports_rerank = __import__(f"{__name__}.infrastructure.ports.rerank", fromlist=["*"])
_sys.modules[__name__ + ".ports.rerank"] = _ports_rerank
_ports_sql = __import__(f"{__name__}.infrastructure.ports.sql", fromlist=["*"])
_sys.modules[__name__ + ".ports.sql"] = _ports_sql


__all__ = [
    # Domain
    "parse_vn_number", "extract_budget", "extract_price_intent", "floor_price_vnd",
    "Offer", "resolve_unit_type_key", "cash_match", "loan_match",
    "affordability_rows", "affordability_summary", "analyze_affordability",
    "offer_from_row",
    "Intent", "ClassifyResult", "classify_intent",
    "RoutedResult", "fallback_route", "rewrite_query",
    "detect_aggregate_intent",
    "HIGH_STAKES_KEYWORDS", "AGGREGATE_KEYWORDS", "GEO_INTENT_KEYWORDS",
    "_normalize_routed",
    "InputGuardResult", "guard_input", "rule_screen",
    "OutputGuardResult", "guard_output", "rerank",
    "LLMClient", "LLMConfigError", "LLMError", "LLMTimeoutError",
    "LazyLLMProxy", "MODEL_ROLE_FIELD", "OpenAICompatibleLLM",
    "get_llm", "llm", "model_for_role",
    "Sqlnl2sqlError", "validate_sql", "extract_sql", "run_nl2sql", "close_nl2sql_pool",
    "sha256_hex", "utc_now_iso", "safe_float", "truncate_str", "slugify",
    # Application
    "SqlLegResult", "SpecError", "SqlLegError", "build_dsn",
    "get_ro_pool", "close_ro_pool", "run_sql_leg",
    "ALLOWED_SOURCES", "ALLOWED_FIELDS", "ALLOWED_OPS", "OFFER_COLUMNS",
    "RagLegResult", "run_rag_leg", "LIGHTRAG_READY",
    "Merged", "merge_context", "build_rag_context", "build_evidence_context",
    "build_sources", "build_facts", "hydrate_chunks",
    "stream_answer", "build_messages",
    "write_audit", "close_audit_pool", "redact_sql_spec", "redact_sql_query",
    "RagQueryWorkflow", "QueryRejected", "STEP_TIMEOUTS",
    "GuardedEv", "RagRequestEv", "SqlRequestEv", "GeoRequestEv",
    "RagDoneEv", "SqlDoneEv", "GeoDoneEv", "MergedEv", "GeneratedEv", "parse_as_of",
    # Infrastructure
    "Settings", "get_settings", "export_runtime_env",
    "get_llm", "get_reranker", "get_geo", "get_rag", "get_sql", "model_for_role",
    "GeoPlace", "GeoPort", "GeoResult", "LLMChatPort",
    "RagChunk", "RagPort", "RagResult", "RerankPort", "SqlPort", "SqlResult",
    "GooglePlaces", "HttpRerank", "LightRag", "NoopRerank",
    "LLMConfigError", "LLMError", "LLMTimeoutError",
    "OpenAICompatibleLLM", "PostgresSql", "StaticPlaces",
    # Interface
    "create_app", "app",
]
