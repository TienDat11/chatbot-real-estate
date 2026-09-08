"""Shared constants: model names, timeouts, SSE events, endpoint paths.

Centralizes magic numbers/strings so provider or policy changes touch one file.
"""

from __future__ import annotations

# --- Timeouts (seconds) ---
# [OI] client default per request. Non-streaming calls that do not override
# it are bounded here; streaming generation is per-read, not totalled, so a
# long answer stream is never cut by this value. 20s still allows the observed
# rewrite/nl2sql single calls (median <5s) while shrinking the worst-case cap.
DEFAULT_LLM_TIMEOUT_S = 20.0
DEFAULT_RERANK_TIMEOUT_S = 3.0  # HTTP rerank call budget (measured 0.3-0.5s)
# per-operation LLM call budget (rewrite / nl2sql). Observed per-step latencies
# are <=9.3s warm; 12s keeps >=1.3x headroom over that while bounding stuck calls.
LLM_CALL_TIMEOUT_S = 12.0
# Hard total deadline (seconds) for ONE whole SSE /query stream, anchored at
# the stream start. The per-step/per-read timeouts above bound each leg, but
# nothing bounded the WHOLE stream: a pipeline that hangs (provider stall that
# still emits a token within llm_timeout_s, an unbounded post-pipeline await,
# a workflow step that ignores cancellation) would otherwise let the SSE loop
# emit heartbeats forever and the FE spin on "Đang nhận câu trả lời…".
# Heartbeats MUST NOT reset this deadline — it is a wall clock, not an idle
# timer. Kept below the FE hard cap (180s) so the backend ships the terminal
# error+done frame first. Slow cold starts (40s+ pre-token silence) fit.
SSE_TOTAL_TIMEOUT_S = 150.0
# Stable error code for the terminal timeout frame; the FE keeps the partial
# answer and offers retry (same path as a mid-stream network cut).
SSE_STREAM_TIMEOUT_CODE = "STREAM_TIMEOUT"
SSE_STREAM_TIMEOUT_MESSAGE = "Máy chủ đang quá tải, chưa kịp trả lời. Vui lòng thử lại."

# --- Query limits ---
MAX_QUERY_LENGTH = 2000  # Pydantic cap on /query input
MAX_INPUT_CHARS = 2000  # L1 rule cap on raw input

# --- SSE event names (order: places -> sources -> facts -> images -> token -> done) ---
# error emitted before done on failure.
SSE_EVENT_PLACES = "places"
SSE_EVENT_SOURCES = "sources"
SSE_EVENT_FACTS = "facts"
SSE_EVENT_IMAGES = "images"
SSE_EVENT_TOKEN = "token"
SSE_EVENT_DONE = "done"
SSE_EVENT_ERROR = "error"

# --- Default model names (MVP provider contract) ---
DEFAULT_MODEL_ANSWER = "gemini-3.6-flash"
DEFAULT_MODEL_ANSWER_PRO = "gemini-3.6-flash"
DEFAULT_MODEL_EXTRACT = "gemini-3.6-flash"
DEFAULT_MODEL_GUARD = "gemini-3.6-flash"
DEFAULT_MODEL_NL2SQL = "gemini-3.6-flash"
DEFAULT_MODEL_REWRITE = "gemini-3.6-flash"

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
RERANK_BINDINGS: tuple[str, ...] = ("dashscope", "aibox", "openrouter")
DEFAULT_RERANK_MODEL = "qwen3-rerank"
RERANK_ENDPOINT_DASHSCOPE = "/v1/reranks"
RERANK_ENDPOINT_AIBOX = "/v1/rerank"

# --- Embedding ---
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_DIM = 1024

# --- Revision-3 outbound (embedding + rerank via OpenRouter-compatible gateway) ---
# The dimension lock is project-wide: 1024 in, 1024 out, every provider. A drift
# corrupts cosine comparability with the corpus and must fail loudly.
EMBEDDING_DIMENSION_LOCK = 1024
# OpenAI embeddings contract: request truncated-to-1024 dims and dense floats so
# vectors arrive in a deterministic, comparable numeric form.
EMBEDDING_ENCODING_FORMAT = "float"
# Batched input cap: one HTTP request carries at most this many texts.
EMBEDDING_BATCH_SIZE = 64
EMBEDDING_HTTP_TIMEOUT_S = 15.0
EMBEDDING_RETRY_ATTEMPTS = 2  # extra attempts after the first try
EMBEDDING_RETRY_BACKOFF_SECONDS = 0.5  # base for exponential backoff
# Model slugs on the OpenRouter-compatible gateway (opt-in bindings).
EMBEDDING_MODEL_OPENROUTER = "openai/text-embedding-3-small"
RERANK_MODEL_OPENROUTER = "voyageai/rerank-2.5"
RERANK_ENDPOINT_OPENROUTER = "/api/v1/rerank"
RERANK_HTTP_TIMEOUT_S = 3.0  # same budget as the legacy rerank call
RERANK_RETRY_ATTEMPTS = 1  # rerank is fail-degraded; a single retry at most
# Only these statuses justify a retry — retrying 4xx validation/auth failures
# would just burn the latency budget on a permanently rejected request.
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
# SSRF guard: outbound embedding/rerank calls may only target allowlisted
# public hosts (exact hostname match, case-insensitive). The Gemini [OI]-compat
# host is added for the Google embedding route.
OUTBOUND_URL_ALLOWED_HOSTS = ("openrouter.ai", "generativelanguage.googleapis.com")

# --- RAG leg token budgets ---
DEFAULT_MAX_ENTITY_TOKENS = 2000
DEFAULT_MAX_RELATION_TOKENS = 2000
DEFAULT_MAX_TOTAL_TOKENS = 6000
