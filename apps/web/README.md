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

## Firestore realtime/CRM base (Epic 8-11 wave 2)

Nền tảng realtime/CRM transport-agnostic đã có sẵn dưới
`src/domain/` + `src/application/` + `src/infrastructure/` + `src/lib/realtime/`
(Firestore hôm nay, WebSocket/Socket.IO sau — swap walkthrough ở
`src/lib/realtime/README.md`).

Cấu hình Firestore nằm ở `firestore.rules` (rules prototype, ghi chú open
question Q7 về backend writer) và `firestore.indexes.json` (composite index cho
`streamLeadsByProject`). Deploy bằng Firebase CLI — KHÔNG deploy trong wave này:

```bash
# từ apps/web (hoặc có firebase.json trỏ vào đây)
npx firebase deploy --only firestore:rules
npx firebase deploy --only firestore:indexes
```

Xem chi tiết: [README repo root](../../README.md).
