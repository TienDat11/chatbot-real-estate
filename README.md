# rag-real-estate — RAG legal real-estate search (basesource)

Chat bot hỗ trợ nhân viên môi giới tra cứu nhanh tài liệu pháp lý nhà đất (văn bản pháp luật,
quy hoạch, hồ sơ dự án, chính sách giá bán) để trả lời khách hàng.

## Status (basesource)

- **Verified:** BE `python -m compileall -q api ingest eval` + import-smoke PASS · FE `next build`
  + `typecheck` PASS · eval offline dry-run (harness self-test only — chưa gọi LLM thật).
- **NOT runtime-verified:** schema/seed SQL chưa từng chạy trên DB thật; chưa có lần gọi
  LLM/rerank/embedding trực tiếp nào; chưa có `.env` secrets (chỉ có `.env.example`).
- **Blocked on:** docker + PostgreSQL + API keys trước khi chạy runtime được.
- **Run bên dưới là đường chạy dự kiến (intended path), không phải một lần chạy đã ghi nhận
  (recorded run).**

## Architecture

### Backend — FastAPI 8-step query pipeline
```
POST /query  →  input guard → rewrite/route → (RAG leg ∥ SQL leg) → rerank → merge
              → generate (SSE stream) → output guard → audit
```
- **RAG leg**: LightRAG graph + vector retrieval (PostgreSQL single-backend)
- **SQL leg**: structured facts (giá, chính sách vay) — spec-builder giới hạn field/op, chạy
  với RLS role `ro_query`, LLM không tự tính toán
- **External services qua Ports & Adapters**: `api/ports/` (Protocol) + `api/adapters/`
  (OpenAI-compatible LLM, HTTP rerank, no-op) — đổi provider chỉ đổi 1 adapter
- **Anti-hallucination**: citation + confidence 3-tier + output guard + audit log

### Database — PostgreSQL (schema v2)
- `documents` / `document_chunks` — registry tài liệu + chunk (metadata `effective_from/to`)
- `facts` / `fact_subjects` — structured facts, interval validity, NUMERIC kỷ luật
- `v_unit_offers` — derived view (affordability), `security_invoker` qua RLS
- Query chạy `SET LOCAL ROLE ro_query` trong transaction

### Frontend — Next.js 15 (monorepo, npm workspaces)
- Workspace names: `@rag-ragre/web` · `@rag-ragre/contracts` · `@rag-ragre/ui`
- `apps/web` — chat UI: SSE streaming (sources → facts → token → done), citation, confidence,
  review banner (Ant Design v5 + Tailwind)
- `packages/contracts` — TypeScript API contract types + constants (single source)
- `packages/ui` — presentational components

### Eval
- Golden set 32 câu (faithfulness / numeric-exact / latency) + prompt-injection test VN (20 câu)

## Run

```bash
# 1. Dev DB — áp schema + seed theo thứ tự (xem db/seed/README.md)
docker compose up -d
psql -f db/schema.sql && psql -f db/audit.sql \
  && psql -f db/seed/legal_docs.sql && psql -f db/seed/price_campaigns.sql \
  && psql -f db/seed/policy_vay.sql   # optional — policy đã có trong price_campaigns.sql
psql -f scripts/verify_ingest.sql     # verify seed

# 2. Backend (copy .env.example → .env, điền secret)
.venv/Scripts/python -m uvicorn api.main:app --port 8000

# 3. Frontend
npm run dev:web   # → http://localhost:3000
```

## Quality gates (bắt buộc trước khi push PR)

- BE: `python -m compileall -q api ingest eval` + import-smoke PASS
- FE: `npm run build --workspace=@rag-ragre/web` + `npm run typecheck` PASS
- Format: `.editorconfig` (LF, final newline) · `.prettierrc.json` (TS/JS) · `pyproject.toml` (ruff: line-length 100)
- PR title: `[Status] <mô tả rõ ràng>` theo convention (xem CLAUDE.md)
