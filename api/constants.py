"""Shared constants: model names, timeouts, SSE events, endpoint paths.

Centralizes magic numbers/strings so provider or policy changes touch one file.
"""

from __future__ import annotations

# --- Timeouts (seconds) ---
DEFAULT_LLM_TIMEOUT_S = 30.0  # OpenAI client default per request
DEFAULT_RERANK_TIMEOUT_S = 3.0  # HTTP rerank call budget
LLM_CALL_TIMEOUT_S = 20.0  # per-operation LLM call budget (rewrite / nl2sql)

# --- Query limits ---
MAX_QUERY_LENGTH = 2000  # Pydantic cap on /query input
MAX_INPUT_CHARS = 2000  # L1 rule cap on raw input

# --- SSE event names (order: places -> sources -> facts -> token -> done) ---
# error emitted before done on failure.
SSE_EVENT_PLACES = "places"
SSE_EVENT_SOURCES = "sources"
SSE_EVENT_FACTS = "facts"
SSE_EVENT_TOKEN = "token"
SSE_EVENT_DONE = "done"
SSE_EVENT_ERROR = "error"

# --- Default model names ---
DEFAULT_MODEL_ANSWER = "deepseek-v4-flash"
DEFAULT_MODEL_ANSWER_PRO = "deepseek-v4-pro"
DEFAULT_MODEL_EXTRACT = "qwen3.7-flash"
DEFAULT_MODEL_GUARD = "deepseek-v4-flash-0731"
DEFAULT_MODEL_NL2SQL = "qwen3.7-flash"
DEFAULT_MODEL_REWRITE = "deepseek-v4-flash"

# --- Role -> Settings field mapping (LLM_MODEL_* env vars) ---
MODEL_ROLE_FIELD: dict[str, str] = {
    "rewrite": "llm_model_rewrite",
    "extract": "llm_model_extract",
    "answer": "llm_model_answer",
    "answer_pro": "llm_model_answer_pro",
    "guard": "llm_model_guard",
    "nl2sql": "llm_model_nl2sql",
}
SUPPORTED_ROLES: tuple[str, ...] = tuple(MODEL_ROLE_FIELD)

# --- Rerank ---
RERANK_BINDINGS: tuple[str, ...] = ("dashscope", "aibox")
DEFAULT_RERANK_MODEL = "qwen3-rerank"
RERANK_ENDPOINT_DASHSCOPE = "/v1/reranks"
RERANK_ENDPOINT_AIBOX = "/v1/rerank"

# --- Embedding ---
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
DEFAULT_EMBEDDING_DIM = 1024

# --- RAG leg token budgets ---
DEFAULT_MAX_ENTITY_TOKENS = 2000
DEFAULT_MAX_RELATION_TOKENS = 2000
DEFAULT_MAX_TOTAL_TOKENS = 6000
