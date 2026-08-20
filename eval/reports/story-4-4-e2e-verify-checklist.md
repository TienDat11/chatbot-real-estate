# Story 4.4 — E2E & Live Smoke Verification Checklist (chờ quota hồi)

> **Ngày ghi:** 2026-08-18
> **Trạng thái:** Story 4.4 (query understanding v2: khẩu ngữ + synonyms) đã code + unit test xong.
> Live smoke (LLM thật) CHƯA chạy vì quota AI Box (api.ai-box.vn) đang hết/âm.
> **Làm các bước dưới sau khi reset key/quota** để nghiệm thu E2E.

---

## 0. Tiền đề (đã xong, không cần làm lại)

- api/domain/services/synonyms.py — SALES_SYNONYMS (29 cặp) + enrich_hl_keywords (ADD-only).
- rewrite.py — enrich hl_keywords trong _normalize_routed.
- rewrite_fewshot.md — thêm Example 6-9 (khẩu ngữ).
- Unit test: tests/test_synonyms.py 14 passed; rewrite+route 39 passed.
- Report deterministic: eval/reports/story-4-4-query-understanding-2026-08.md.

---

## 1. Cập nhật key (sau khi quota hồi)

Mở .env, điền key mới:

    LLM_API_KEY="<AIBOX_KEY_MOI>"
    RERANK_API_KEY="<AIBOX_KEY_MOI>"
    EMBEDDING_API_KEY="<AIBOX_KEY_MOI>"

---

## 2. Khởi động môi trường

    docker compose -p rag-real-estate up -d
    .venv/Scripts/python -m uvicorn api.interfaces.api.main:app --host 127.0.0.1 --port 8000 --reload
    npm run dev:web

Kiểm tra: http://127.0.0.1:8000/health và /ready.

---

## 3. Live smoke 10 câu khẩu ngữ (BẮT BUỘC — acceptance §5)

Gửi từng câu qua POST /query (SSE) và kiểm tra: rewritten tự chứa, đúng leg, hl_keywords enrich đúng.

| # | Câu khẩu ngữ | Kỳ vọng routing | Kỳ vọng hl_keywords |
|---|---|---|---|
| 1 | nhà 2 ngủ buzview biển còn không con | pricing/spec | 2PN, view biển |
| 2 | tụi tui có 3 tỉ thôi mua được gì hông | affordability | budget 3 tỷ |
| 3 | trả góp 0% là sao, có bị lừa hông | needs_rag (KHÔNG high-stakes) | HTLS, 0% |
| 4 | tầng mấy đẹp nhất giá tốt | pricing tier | — |
| 5 | căn góc có view biển không bà con | spec | view biển, căn góc |
| 6 | cho hỏi cọc bao nhiêu là giữ được căn | price | tiền cọc |
| 7 | bên mình có hỗ trợ trả chậm không | needs_rag | HTLS, 0% |
| 8 | tui muốn mua để cho thuê, căn nào hợp | spec | purpose=invest |
| 9 | đóng sớm 95% được giảm bao nhiêu | price | sớm 95 |
| 10 | bàn giao khi nào vậy em | needs_rag | bàn giao |

Script nhanh (chạy từng câu):

    .venv/Scripts/python -c "import httpx; q='nhà 2 ngủ buzview biển còn không con'; r=httpx.stream('POST','http://127.0.0.1:8000/query',headers={'Accept':'text/event-stream'},json={'query':q},timeout=60.0); [print(l) for l in r.iter_lines() if l]"

**Checklist mỗi câu:**
- [ ] Event routing xuất hiện đầu tiên, intent đúng.
- [ ] Answer có citation [fe-xxx] (không bịa số).
- [ ] Không em-dash, không cụm máy móc.
- [ ] Khẩu ngữ được rewrite tự chứa (không giữ nguyên câu thô).

---

## 4. Regression (deterministic, không tốn token)

    .venv/Scripts/python -m pytest tests/ -q
    .venv/Scripts/python -m compileall -q api ingest eval
    .venv/Scripts/python eval/run_eval.py --dry

- [ ] pytest toàn bộ xanh (233+ câu, gồm 14 test_synonyms mới).
- [ ] eval --dry PASS.

---

## 5. Ghi kết quả

Sau khi chạy xong, ghi kết quả thật vào eval/reports/story-4-4-query-understanding-2026-08.md
(thay dòng "Live smoke ... chạy ở 4.7" bằng bảng PASS/FAIL thật + latency).
