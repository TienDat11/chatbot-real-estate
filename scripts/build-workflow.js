export const meta = {
  name: "ragre-basesource-build",
  description:
    "Build rag-real-estate basesource: 4 parallel agent groups (ingest, api, eval+scripts, FE monorepo) writing disjoint paths per plan interface contract",
  phases: [{ title: "Build", detail: "4 parallel agents write BE+FE modules" }],
};

const path = require("node:path");
const ROOT = path.resolve(__dirname, "..");
const PLAN = path.resolve(ROOT, ".claude/plans/rag-real-estate-final.plan.md");
const SCHEMA = path.join(ROOT, "db/schema.sql");
const ENVEX = path.join(ROOT, ".env.example");
const REQ = path.join(ROOT, "requirements.txt");
const PROMPTS = path.join(ROOT, "prompts/system_policy.md");
const REWRITE_PROMPT = path.join(ROOT, "prompts/rewrite_fewshot.md");

const COMMON = [
  "You are building part of a production-grade FastAPI + LightRAG + LlamaIndex basesource for a Vietnamese legal real-estate RAG chatbot.",
  "MANDATORY: First read " +
    PLAN +
    " (the authoritative spec) sections named in your task, plus " +
    SCHEMA +
    ", " +
    ENVEX +
    ", " +
    REQ +
    ", and any prompt files named.",
  "Rules for ALL files you write:",
  "- Vietnamese comments/docstrings where natural (technical terms stay English). UTF-8.",
  "- Clean architecture: routers thin, services/plain functions hold logic (typed response models, dependency aliases, service layer, no business logic in route handlers).",
  "- All env vars come from ingest/config.py Settings (pydantic-settings) — NEVER hardcode secrets/urls. Never commit real keys.",
  "- Async everywhere (asyncpg / openai async). Deterministic ordering. Handle errors explicitly, no silent swallows; degrade with flags (rag_degraded/sql_degraded/router_degraded) instead of crashing.",
  "- Write COMPLETE, importable files — no stubs, no TODOs-as-code. If something genuinely needs runtime verification (LightRAG signature, llama-index API), code defensively with try/except fallback and a clear comment marking the spike item.",
  "- Do NOT touch files outside your assigned paths.",
  "Return structured output: files you created, key function signatures, and any notes/decisions.",
].join("\n");

const INGEST_CONTRACT = [
  "TASK: implement the ingest pipeline (plan §3.2, §3.7, §10 Ngày 1-4). Create exactly these files:",
  "- ingest/__init__.py",
  "- ingest/config.py — SINGLE Settings via pydantic-settings. Fields must cover every var in " +
    ENVEX +
    " plus: rag_max_entity_tokens=2000, rag_max_relation_tokens=2000, rag_max_total_tokens=6000, chunk_cap=1200, extract_timeout, lightrag_workspace. Compute properties: pg_dsn (asyncpg), pg_dsn_sync (psycopg2), pg_dsn_ro (same, used with SET LOCAL ROLE ro_query). Singleton settings. This file is imported by EVERY other module (api + ingest).",
  "- ingest/lightrag_init.py — def get_lightrag() -> LightRAG (process-wide singleton, init lazily). Configure LightRAG(working_dir=settings.lightrag_workspace, embedding_func=..., embedding_bindings=..., embedding_binding_name=settings.embedding_binding, embedding_dim=1024, llm_model_func=..., llm_model_name=..., llm_model_kwargs={base_url: settings.llm_base_url, api_key: settings.llm_api_key}, storage=PostgresStorage, kv_storage=PostgresKVStorage, doc_status_storage=PGDocStatusStorage, graph_storage=PGTableGraphStorage, vector_storage=PGVectorStorage, addon_params={language: Vietnamese, entity_type_prompt_file: prompts/entity_type/legal_vn.yml, entity_extraction_use_json: True}, chunk_token_size=settings.chunk_cap, chunk_overlap_token_size=50, enable_llm_cache=True, max_async=6, max_parallel=2). Embedding: try aibox/dashscope openai-compatible client (text-embedding-v4 via settings.embedding_base_url), fallback local to a stub that logs a loud warning (local needs a real local model — mark spike). Also expose async def ainsert_document(rag, doc_id, chunks) used by load.py: calls rag.ainsert(chunks, ids=[chunk_id...], file_paths=[doc_id,...]) with a chunking_func defined inline that returns the chunk as-is (passthrough — LightRAG must NOT re-chunk; verify signature at runtime, fallback config). Wrap in try/except with clear error message.",
  "- ingest/parser.py — async def parse_document(path, kind) -> ParsedDoc using docling (DocumentConverter) to extract text + tables; split legal docs by Điều/Khoản into pre-chunks capped at settings.chunk_cap chars; returns dataclass ParsedDoc(doc_id, title, kind, sections: list[ParsedSection(text, section_title, tables)], content_hash=sha256 of raw text). Handle PDF errors gracefully -> raise ParserError with message.",
  "- ingest/fact_extract.py — async def extract_facts(text, doc_id, kind) -> list[ExtractedFact] calling extraction LLM (qwen3.7-flash via a small openai async client json mode, settings.llm_api_key/base_url) with a Pydantic v2 schema: ExtractedFact(fact_key, subject_key, subject_type, subject_display, value_num, value_text, unit, quality, range_min, range_max, policy_key, campaign_key?, extract_conf, span). Also deterministically extract from tables (rows -> price_vnd/deposit_pct/term_months/interest_rate_pct facts). Retry once on invalid JSON; low conf or failure -> append to review_queue (fact_extract kind) via SQL, do NOT guess. Include NUMBER_PATTERNS + parse_vn_number(text) -> Decimal|None handling 2,85 tỷ / 2.850.000.000đ / 85,5 m² / một tỷ hai (number-word map for common words).",
  "- ingest/placeholder.py — constants FACT_TOKEN_START=⟦FACT:, FACT_TOKEN_END=⟧. sanitize_forged_tokens(text) escapes literal ⟦⟧ already in source. replace_fact_with_placeholder(text, spans) -> (text, refs) where refs = list of (chunk_id_hint, subject_key, fact_key, policy_key). resolve_placeholders(text, resolver) -> str (hydrate: for each ⟦FACT:key@subject⟧ resolve via resolver callable; missing/expired -> marker [không có dữ liệu hiệu lực]).",
  "- ingest/load.py — async def load_document(parsed: ParsedDoc) -> LoadResult. THE critical transaction (plan §3.2 step 5): single asyncpg transaction: upsert documents (ON CONFLICT doc_id DO UPDATE version+1, status published, effective range) -> upsert fact_subjects (subject_key normalized: lowercase, strip dots/dashes) -> insert document_chunks (chunk_id = f(doc_id, version, index), text_hash sha256) -> insert facts (inherit effective range from doc/campaign; campaign upsert) -> insert chunk_fact_refs -> insert ingest_log -> COMMIT. AFTER commit: call ainsert_document (own pool) so LightRAG indexes the placeholder-containing chunks; store lightrag_doc_id back. On failure: rollback + raise with context. Also async def expire_facts(subject_id, fact_key, policy_key, as_of) helper for updates.",
  "- scripts/verify_ingest.sql is created by ANOTHER agent — do not create it.",
  "Read " + SCHEMA + " for exact table/column names. Read " + PLAN + " §3.2 and §3.7 carefully.",
].join("\n");

const API_CONTRACT = [
  "TASK: implement the query API per plan §4 (8-step pipeline), §16.2-16.4 (API contract), clean architecture. Create exactly these files under api/:",
  "- api/__init__.py",
  "- api/llm.py — class LLMClient built on openai>=2 AsyncOpenAI: async def complete(messages, *, json_mode=False, model=None, max_tokens=None, timeout=30) -> str (json_mode -> response_format json_object), async def stream(messages, *, model=None) -> AsyncIterator[str]. Singleton llm = LLMClient(...) from settings. Used by ALL other api modules. Model env vars: rewrite->LLM_MODEL_REWRITE, extract->LLM_MODEL_EXTRACT, answer->LLM_MODEL_ANSWER, answer_pro->LLM_MODEL_ANSWER_PRO, guard->LLM_MODEL_GUARD, nl2sql->LLM_MODEL_NL2SQL.",
  "- api/guard_input.py — async def guard_input(raw: str) -> GuardResult dataclass(clean: str, rejected: bool, reason: str|None). L1: rules (len<=2000, pattern exfiltration), optional Prompt-Guard-2 call via httpx if GUARD_INPUT_PG2_URL env set (else rule-only + flag). Rejects -> caller returns 400 + audit.",
  "- api/rewrite.py — async def rewrite_query(query, history, as_of) -> RoutedResult dataclass(rewritten, routing: dict, sql_spec: dict|None, hl_keywords, ll_keywords, high_stakes, as_of, degraded). Single LLM call deepseek-v4-flash JSON mode with few-shot from " +
    REWRITE_PROMPT +
    ". Parse VN amounts (dưới 2 tỷ -> <=2000000000; tôi có X tiền -> budget -> required_down_payment_vnd). Invalid JSON -> retry 1 -> fallback rag-only + router_degraded=True. Multi-turn: history<=4 turn/800 tokens, query must be self-contained. High-stakes keyword set from plan §4.7. INCLUDE deterministic intent detector here: def detect_aggregate_intent(query) -> bool (keywords: bao nhiêu căn / trung bình / tổng / so sánh / giá trên m2 ...) — nl2sql path ONLY if this matches.",
  "- api/rag_leg.py — async def run_rag_leg(rewritten, hl, ll, as_of) -> RagLegResult dataclass(chunks: list[dict], degraded: bool, error: str|None). Uses ingest/lightrag_init.get_lightrag().aquery(rewritten, QueryParam(mode=hybrid, only_need_context=True, hl_keywords=hl, ll_keywords=ll, enable_rerank=False, max_entity_tokens=settings.rag_max_entity_tokens, max_relation_tokens=settings.rag_max_relation_tokens, max_total_tokens=settings.rag_max_total_tokens)). Read raw_data.chunks (id, score, content). Then POST-FILTER each chunk: JOIN documents status=published + as_of interval via SQL (asyncpg ro pool) — drop expired. Timeout/error -> degrade (chunks=[], degraded=True) NOT crash.",
  "- api/sql_leg.py — R1 spec-builder PRIMARY. Constants: ALLOWED_FIELDS = {v_unit_offers: [price_vnd, deposit_pct, term_months, interest_rate_pct, required_down_payment_vnd, loan_amount_vnd, monthly_principal_vnd, monthly_interest_estimate_vnd, policy_key, subject_id], facts: [price_vnd, area_m2, deposit_pct, term_months, interest_rate_pct, subject_key, subject_type, value_num, value_text, unit, quality, policy_key, campaign_key]}; ALLOWED_OPS={=,!=,<,<=,>,>=,between,in}; ALLOWED_DIR={asc:ASC, desc:DESC}; LIMIT 1..20. def validate_spec(spec) -> None raises SpecError on closed-set violation. async def run_sql_leg(spec, as_of, query) -> SqlLegResult dataclass(rows: list[dict], meta: dict, degraded: bool): build parameterized SQL, run inside a transaction helper with_rls_identity() that: BEGIN -> SET LOCAL statement_timeout=2s -> SET LOCAL ROLE ro_query -> run -> COMMIT. Implement match_semantics for range/approx (<= matches if range_min<=value; between=interval overlap; in=categorical only). Rows -> FACT_EVIDENCE blocks fe-001..: value, quality, range, effective dates, source_doc_id, campaign_key, formula note. R2: import async def run_nl2sql(query, as_of) -> SqlLegResult from api/nl2sql_guard.py and call it when spec.structured_path==nl2sql.",
  "- api/nl2sql_guard.py — R2 gated: async def run_nl2sql(query, as_of) -> SqlLegResult. Guardrails: build prompt -> call LLM (LLM_MODEL_NL2SQL) to produce SQL over ONLY v_unit_offers + campaigns -> validate with sqlglot AST: exactly 1 SELECT, no semicolon/comment/multi-statement, no DML, tables in closed whitelist, no information_schema/pg_catalog, function allowlist (reject pg_sleep/set_config), wrap SELECT * FROM (sql) q LIMIT 20. Execute via dedicated ro_nl2sql engine (asyncpg, default_transaction_read_only=on, statement_timeout=8s) with SET LOCAL ROLE ro_query. Return raw rows + metadata (sql_query redacted for audit). Any validation fail -> raise Sqlnl2sqlError -> caller degrades to RAG-only + audit.",
  "- api/rerank.py — async def rerank(query, chunks) -> list[dict] (app-side, THE single score source). If ENABLE_RERANK and RERANK_BINDING in (dashscope,aibox): POST to rerank endpoint (dashscope /v1/reranks or aibox /v1/rerank with RERANK_BINDING=cohere) httpx json {model, query, documents:[content], top_n}. Map scores back to chunks. On failure/off -> return chunks with score as-is + flag. RAG chunks only — never rerank FACT_EVIDENCE.",
  "- api/merge.py — async def merge_context(query, rag_chunks, rows, as_of) -> Merged dataclass(rag_blocks: str, evidence_blocks: str, sources: list[dict], facts: list[dict]). Build RAG_CONTEXT block (delimiter + JSON-encoded chunks with doc metadata hydrated from registry incl. title/effective dates) and FACT_EVIDENCE block (JSON-encoded fe blocks). sources[] for UI: {doc_id, title, section, effective_from, kind}. facts[] for UI table: {fe_id, subject, policy_key, fields{...}, note}.",
  "- api/generate.py — async def stream_answer(merged: Merged, history, high_stakes) -> AsyncIterator[str] streaming tokens from LLM (LLM_MODEL_ANSWER, or LLM_MODEL_ANSWER_PRO if high_stakes). System prompt from " +
    PROMPTS +
    " read once at import. Messages: system > user(rewritten+history<=4) > data messages (RAG_CONTEXT + FACT_EVIDENCE) — NEVER concat system. Also returns final meta via attribute.",
  "- api/guard_output.py — async def guard_output(answer, facts, sources, routing) -> GuardResult dataclass(confidence: str, requires_review: bool, verdicts: dict). L4: (a) numeric grounding — every financial number in answer must byte-match a FACT_EVIDENCE value (normalized); (b) orphan numbers -> LOW + review flag; (c) citation-grounding best-effort (span in hydrated chunk); (d) confidence 3-tier: HIGH = grounding pass + (SQL>=1 row provenance OR >=2 chunks rerank>=0.8) + no inconsistent; MEDIUM = 1 source/degraded/approx; LOW -> requires_review. high_stakes keyword match ALSO sets requires_review.",
  "- api/audit.py — async def write_audit(entry: dict) asyncpg INSERT into query_audit (append-only role ragre). Redact sql_spec literals + sql_query before write. Compute sha256 hashes (prompt_hash, answer_hash). Never fails the pipeline (try/except log).",
  "- api/workflow.py — THE ORCHESTRATOR. Plan §4.0: implement the 8-step pipeline as an ASYNC orchestrator (asyncio.gather for the two legs — this is the documented fallback for the llama-index Workflows API which must be spike-verified first; structure it so a LlamaIndex Workflows wrapper can be added later). class RagQueryPipeline: async def run(self, query, session_id, as_of, history) -> dict returning the FULL §16.2 payload {answer, sources, facts, confidence, requires_review, routing, trace_id, latency_ms} + internal trace. Steps with per-step timeouts (guard 1s, rewrite 4s, rag 6s, sql 2s/8s, rerank 3s, generate stream, output_guard 2s) using asyncio.wait_for; any step timeout -> degrade flag + continue (never crash). trace_id = uuid4 hex short. Emit intermediate results via a callback on_event(event_name, data) so main.py can stream SSE.",
  "- api/main.py — FastAPI app factory (create_app) + module-level app. CORS per settings. Lifespan: no eager LightRAG init. Routes: POST /query (if client Accept: text/event-stream -> SSE stream with events sources, facts, token, done, error in EXACT order; else return full JSON payload). Response models typed (Pydantic) per §16.2: QueryRequest{query, session_id?, as_of?, history?}, QueryResponse{answer, sources[], facts[], confidence, requires_review, routing, trace_id, latency_ms}. GET /health, GET /ready (checks PG reachable + lightrag storages init flag), GET /sources/{doc_id} (registry metadata + status). Audit write in finally. SSE must emit done even after error.",
  "Read " +
    SCHEMA +
    " for exact SQL names. Read " +
    PLAN +
    " §4, §16.2-16.4. Every module = plain functions testable without the framework.",
].join("\n");

const EVAL_CONTRACT = [
  "TASK: implement eval + scripts + db seed per plan §10-11, §16.1. Create exactly these files:",
  "- db/seed/README.md — mô tả seed data + cách nạp.",
  "- db/seed/price_campaigns.sql — 2 campaigns (tower-a-2026q2, tower-b-2026q1 per plan), project documents, fact_subjects + facts for >=20 units (2-3 projects, 1-10 tỷ), each with price_vnd + area_m2; 2 bank policies per unit (bank_a deposit 25 term 180 interest 8.5; bank_b deposit 30 term 240 interest 8.0) + 1 policy 0% deposit + 1 unit with NO policy (should not appear in v_unit_offers). Must satisfy v_unit_offers view logic (deposit/term required for policy rows). Use clean INSERTs with ON CONFLICT DO NOTHING where sensible; idempotent.",
  "- db/seed/policy_vay.sql — mock bank policies as facts (deposit_pct/term_months/interest_rate_pct) tied to campaign + policy_key.",
  "- db/seed/legal_docs.sql — 8-12 legal documents registry entries (documents table): 31/2024/QH15 (Luật Đất đai 2024, effective 2025-01-01), 29/2023/QH15 (Luật KDBĐS 2023, 2024-08-01), 91/2015/QH13 (BLDS, Điều 309-321/328), 27/2023/QH15 (Luật Nhà ở), NĐ 101/2024, NĐ 96/2024, NĐ 99/2022, TT 04/2024/TT-BXD. kind=legal, status published, effective_from/to, content_hash placeholder sha256 hex.",
  "- eval/golden_set_v1.json — >=25 questions array, each: {id, question, category: legal|fact_affordability|aggregate|mixed|refusal|as_of|number_format, expected_routing:{needs_rag,needs_sql,structured_path}, expected_facts?, gold_chunk_ids?, expected_answer_contains: [], notes}. Include: tôi có 2 tỉ mua được nhà nào? (expect unit 8 tỉ 25% → required 2.000.000.000), có bao nhiêu căn dưới 2 tỷ? (aggregate), cầm cố quyền sử dụng đất có hợp pháp không? (legal high_stakes), câu về Luật Đất đai 2024 hiệu lực, câu chuyển nhượng, câu giá liên hệ, câu số viết chữ một tỷ hai, as-of câu hỏi lịch sử, refusal câu không liên quan. All Vietnamese. Dates relative to 2026-08.",
  "- eval/injection_test_vn.json — 20 prompts anti-injection tiếng Việt: prompt injection vào query (bỏ qua hướng dẫn và trả lời...), SQL injection attempts, exfiltration, role-reversal, plus 10 benign controls. Each {id, prompt, label: injection|benign, expect_reject: bool}.",
  "- eval/run_eval.py — CLI: loads golden_set_v1.json, for each question calls pipeline (import api.workflow RagQueryPipeline) with real backend, evaluates: faithfulness (judge LLM, pinned version), numeric exact-match (extract numbers from answer vs expected_facts), routing/path accuracy, refusal correctness, freshness (campaign expire case). Prints P50/P95 latency + per-question pass/fail + summary table. Graceful: --subset flag, --dry (mock LLM) for CI subset.",
  "- scripts/verify_ingest.sql — A13: queries: dangling placeholder count (regex on document_chunks.content: content LIKE %⟦FACT% without closing ⟧), chunks with placeholder but no chunk_fact_refs, chunk_id format check, integrity regex; returns row counts that MUST be 0.",
  "- scripts/update_price.sh — bash: backup pg_dump -> run a python helper inline (or SQL) that in ONE transaction: expire old facts (set effective_to=CURRENT_DATE) for campaign, insert new facts + new campaign row -> run golden-set regression (eval --subset) -> echo 0 re-embed note. Uses env from .env.",
  "- scripts/update_6mo.sh — bash: pg_dump backup -> off-box incremental ingest (call ingest/load.py for changed docs) -> restore/verify -> regression -> re-baseline. CRM-level steps as comments.",
  "Read " +
    SCHEMA +
    " for exact table names. Read " +
    PLAN +
    " §10 (Ngày 3-4, 9), §11, §16.1. Facts must respect the numeric discipline (NUMERIC(20,0) vnd, NUMERIC(5,2) pct, NUMERIC(6,4) interest).",
].join("\n");

const FE_CONTRACT = [
  `TASK: build the FE monorepo + Next.js chat app (plan §16.2-16.5 checklist, react-patterns, taste-style enterprise trust-first design). The Next.js app already exists at ${path.join(ROOT, "apps/web")} (create-next-app, TS, Tailwind v4, App Router, src dir, @/* alias). Node 24 + npm 11 available. DO NOT re-run create-next-app.`,
  `STEP 0 — monorepo root: create ${path.join(ROOT, "package.json")} ({"name":"rag-real-estate","private":true,"workspaces":["apps/*","packages/*"], scripts:{"dev:web":"npm run dev -w @rag-ragre/web","build":"npm run build -w @rag-ragre/web","typecheck":"npm run typecheck -w @rag-ragre/web"}}) + ${path.join(ROOT, "turbo.json")} (simple tasks dev/build/lint) + ensure .gitignore covers node_modules/.next/dist (check existing apps/web/.gitignore first).`,
  "STEP 1 — packages/contracts (npm package @rag-ragre/contracts, TS only, no deps): src/index.ts with EXACTLY the API contract types mirroring FastAPI §16.2: QueryRequest{query, session_id?, as_of?, history?}, QueryHistoryItem{role:user|assistant, content}, Source{doc_id,title,section?,effective_from?,kind}, FactEvidence{fe_id,subject,policy_key?,fields:Record<string,number|string|null>,note?}, QueryResponse{answer,sources,facts,confidence:HIGH|MEDIUM|LOW,requires_review,routing:{needs_rag,needs_sql,structured_path:spec|nl2sql|none},trace_id,latency_ms}, SseEvent{event:sources|facts|token|done|error, data:any}, plus helper formatVND(value:number):string using Intl.NumberFormat(vi-VN) + đ suffix. package.json with types/exports. Build with tsc.",
  "STEP 2 — packages/ui (npm package @rag-ragre/ui, deps: react, antd, @rag-ragre/contracts, dayjs): ThemeProvider (antd ConfigProvider with design tokens), ConfidenceBadge (HIGH emerald / MEDIUM amber / LOW red), ReviewBanner (warning banner Cần tư vấn viên xác nhận), Disclaimer (footer note AI hỗ trợ, không phải tư vấn pháp lý chính thức), SourcesList, FactsTable (renders facts[] as antd Table), MarkdownView (react-markdown wrapper). Export all from index.ts. package.json with peerDeps react>=18, antd>=5.",
  'STEP 3 — apps/web chat app (overwrite src/app/page.tsx + add src/lib + src/components). npm install in apps/web: antd @ant-design/nextjs-registry @ant-design/icons react-markdown remark-gfm @rag-ragre/contracts @rag-ragre/ui dayjs. Wire antd for SSR: src/app/layout.tsx must wrap children in AntdRegistry. next.config.ts add transpilePackages:["@rag-ragre/contracts","@rag-ragre/ui","antd","@ant-design/icons"] and rewrites: [{source:"/api/:path*", destination:"http://localhost:8000/:path*"}] (FastAPI dev on :8000).',
  "APP BEHAVIOR (plan §16.3-16.5): single-page chat: header (bot name + disclaimer tag), message list (user right / assistant left, antd bubbles), streaming answer via SSE. API client in src/lib/api.ts: async function streamQuery(req, handlers) using fetch(/api/query,{method:POST,headers:{Content-Type:application/json,Accept:text/event-stream},body:JSON.stringify(req)}) + ReadableStream + TextDecoder parsing SSE lines (event:/data:/events:); handlers for sources/facts/token/done/error. session_id = crypto.randomUUID() in sessionStorage; client keeps history<=4 turns. Assistant message lifecycle: create bubble immediately -> attach sources (SourcesList) when sources -> attach facts (FactsTable) when facts -> append token text progressively (with typing caret) -> on done set confidence badge + requires_review banner. Formatting: ALL money via formatVND from contracts — NEVER recompute numbers client-side. Shows trace_id + latency in a subtle footer per message. Error: antd message.error toast + keep input. Send on Enter (Shift+Enter newline), disable while streaming.",
  "DESIGN (taste, enterprise-trust): colorPrimary #1F46A8 (trust navy), bg #F7F8FA, Be Vietnam Pro font via next/font/google (fallback Inter), confidence colors: HIGH #16A34A / MEDIUM #D97706 / LOW #DC2626, facts table with tabular-nums + subtle borders, clean radius, generous whitespace, left evidence rail (collapsible) if layout allows. Vietnamese UI copy. src/app/page.tsx must be a client component (use client) with the full chat; split components into src/components/ (ChatPage, MessageList, MessageBubble, Composer, EvidencePanel). Keep files <800 lines, components small.",
  "Verify: run npm install at repo root (workspaces) then npm run build -w @rag-ragre/web until it passes. Fix any TS/build errors. If antd + React versions clash, pin compatible versions (React 19 + antd v5 is fine).",
].join("\n");

const FILES_SCHEMA = {
  type: "object",
  properties: {
    files: {
      type: "array",
      items: { type: "string" },
      description: "Repo-relative paths of files created",
    },
    summary: { type: "string", description: "What was built and key decisions" },
    signatures: {
      type: "array",
      items: { type: "string" },
      description: "Key function/class signatures implemented",
    },
    notes: {
      type: "array",
      items: { type: "string" },
      description: "Open items, spike flags, deviations",
    },
  },
  required: ["files", "summary", "signatures", "notes"],
};

phase("Build");
const results = await parallel([
  () =>
    agent(COMMON + "\n\n" + INGEST_CONTRACT, {
      label: "ingest",
      phase: "Build",
      schema: FILES_SCHEMA,
      agentType: "general-purpose",
    }),
  () =>
    agent(COMMON + "\n\n" + API_CONTRACT, {
      label: "api",
      phase: "Build",
      schema: FILES_SCHEMA,
      agentType: "general-purpose",
    }),
  () =>
    agent(COMMON + "\n\n" + EVAL_CONTRACT, {
      label: "eval+scripts+seed",
      phase: "Build",
      schema: FILES_SCHEMA,
      agentType: "general-purpose",
    }),
  () =>
    agent(FE_CONTRACT, {
      label: "fe-monorepo",
      phase: "Build",
      schema: FILES_SCHEMA,
      agentType: "general-purpose",
    }),
]).then((r) => r.filter(Boolean));

return { results };
