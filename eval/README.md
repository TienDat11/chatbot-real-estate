# Eval — run_eval.py

Bộ đánh giá chatbot RAG bất động sản (plan §10-11, §16.1). Chạy golden set qua
`eval/golden_set_v1.json` và bộ test anti-injection qua `eval/injection_test_vn.json`.

Tài liệu này KHÔNG công bố bất kỳ con số accuracy nào của hệ thống. Mọi ngưỡng gate
đều là hằng số trong code / `_meta` data file — đọc tại đó, đừng copy số vào đây.

## Yêu cầu chạy thật (real verification)

Verification thật cần ĐỦ 4 thứ sẵn sàng cùng lúc:

1. PostgreSQL (schema + seed, gồm chunk content cho judge)
2. Embedding model (dims 1024, khoá theo aibox text-embedding-v4)
3. Reranker
4. Answer model (LLM sinh câu trả lời) + judge model (ghim riêng qua `EVAL_JUDGE_MODEL`)

Thiếu bất kỳ thành phần nào → KHÔNG có verification thật. `--dry` không phải bước
thay thế (xem mục `--dry` ở cuối).

## CLI

```bash
python eval/run_eval.py                          # full golden set, backend thật
python eval/run_eval.py --subset 10              # 10 câu đầu
python eval/run_eval.py --only-category legal    # chỉ 1 category
python eval/run_eval.py --dry                    # CHỈ tự test harness: MockPipeline + MockJudge — không gọi PG/LLM/embedding/rerank
python eval/run_eval.py --inject                 # chạy eval/injection_test_vn.json qua guard
python eval/run_eval.py --json-out eval/results.json
python eval/run_eval.py --fail-fast              # exit 1 nếu có câu fail
```

Flag: `--golden` (mặc định `eval/golden_set_v1.json`), `--subset N`, `--only-category`,
`--dry`, `--inject`, `--json-out PATH`, `--fail-fast`. Env: `POSTGRES_*`, `LLM_BASE_URL`,
`LLM_API_KEY`, `EVAL_JUDGE_MODEL`, `EVAL_PIPELINE_TIMEOUT_S`.

## Exit gates (điều kiện exit code)

Runner có gate ở cuối `amain()`; giá trị ngưỡng nằm TRONG CODE/DATA:

- **Numeric exact-match** — exit code 1 khi tỷ lệ câu khớp số học rơi dưới ngưỡng
  §11 (hằng số trong `amain()`).
- **Faithfulness (unsupported-claim)** — judge LLM chấm từng câu; claim ngoài context →
  câu đó fail (không có exit gate riêng, chỉ tính vào pass rate).
- **Latency** — SUMMARY in P50/P95 so với ngân sách vận hành (hằng số trong
  `_print_summary`); đây là chỉ số hiệu năng, chưa phải exit gate.
- **Injection** — `eval_injection` pass khi tỷ lệ chặn đạt `ok_threshold_pct` khai báo
  trong `_meta` của `injection_test_vn.json`.

## Image relevance (illustrative images)

Golden câu có thể khai báo thêm `expected_images` để chấm payload `images` (ảnh minh
họa từ `search_images`) — regression lock cho bug "hỏi thanh toán ra ảnh mặt bằng".
Hai dạng expectation:

| Dạng | Ý nghĩa | Pass khi |
|---|---|---|
| `{"none": true}` | Câu không được gắn ảnh (off-topic/refusal/legal) | `images` rỗng |
| `{"kinds": ["thanh-toan"]}` | Câu phải có ảnh, kind nằm trong danh sách | `images` không rỗng và mọi kind ⊆ expectation |

Metric `images (kinds)` trong SUMMARY = tỷ lệ câu **có khai báo** `expected_images`
pass (câu không khai báo thì bỏ qua, không vào mẫu số). Câu fail in kèm lý do:
`images: expect none nhưng có N ảnh kinds=[...]` hoặc `images: kind ngoài kỳ vọng
[...] (want [...])`.

Cách đọc:
- `{"none": true}` fail = ảnh rác bị gắn vào câu không liên quan (regression ngược bug).
- `{"kinds": [...]}` fail = hoặc recall thiếu (không có ảnh nào), hoặc precision kém
  (kind lạ lẫn vào). Đối chiếu thêm score thật trong integration suite
  `tests/test_integration_image_search.py`.
- Chiều này chỉ chấm **kind**, không chấm đúng image cụ thể — việc khóa căn exact
  (match = exact) do integration suite lo: `test_unit_query_*`.

## Injection test contract

`eval/injection_test_vn.json` chứa các prompt tiếng Việt hai nhóm: injection và benign
control (số lượng từng nhóm đọc trong `_meta` / mảng `prompts` của file). Mỗi prompt có
`{id, prompt, label: injection|benign, expect_reject}`. Contract: injection phải bị chặn
(`expect_reject=true`), benign KHÔNG được reject (`expect_reject=false`). Chạy bằng
`--inject`; ghi FP/FN khi chạy. Pass khi tỷ lệ chặn đạt `ok_threshold_pct` trong `_meta`.

## ⚠️ `--dry` — harness self-test, KHÔNG phải verification pipeline

`--dry` chạy **`MockPipeline` + `MockJudge`**: `MockPipeline.run()` trả payload được
tổng hợp từ chính expectation của golden câu (`expected_answer_contains`,
`expected_facts`, `expected_images`), `MockJudge.judge()` luôn verdict "supported".
Vì mock tự khớp kỳ vọng, điểm SUMMARY khi `--dry` **luôn đẹp một cách giả tạo**.

`--dry` chỉ chứng minh đúng một điều: **harness wiring chạy được** — CLI parse flag,
golden JSON đọc được, các checker (`numeric_exact_match`, `_check_routing`,
`_check_images`, `_check_refusal`, `_check_freshness`, `_persona_checks`) gọi được và
tính tổng/exit code đúng. Nó **không** chứng minh:

- pipeline thật trả lời đúng,
- embedding / retrieval / reranker hoạt động,
- faithfulness hay confidence của câu trả lời thật,
- ảnh minh hoạ hay routing thật khớp kỳ vọng.

Verification pipeline thật bắt buộc chạy real run (không `--dry`) với PostgreSQL +
embedding model + reranker + answer model + judge model. Kết quả `--dry` không được
dùng làm bằng chứng chất lượng ở bất kỳ đâu.
