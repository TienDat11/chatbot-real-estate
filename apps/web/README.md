# @rag-ragre/web — Chat UI cho RAG legal real-estate

Chat UI cho backend RAG pháp lý bất động sản (`api/` — FastAPI 8-step pipeline): SSE streaming
(sources → facts → token → done), citation, confidence badge, review banner.

## Dev

```bash
# từ repo root (monorepo npm workspaces)
npm install
npm run dev:web   # → http://localhost:3000
```

Next.js proxy chuyển `/api/*` → FastAPI `:8000` (xem `next.config.ts`). Backend phải chạy trước:
`.venv/Scripts/python -m uvicorn api.main:app --port 8000`.

Xem chi tiết: [README repo root](../../README.md).
