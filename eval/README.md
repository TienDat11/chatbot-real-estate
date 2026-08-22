# Eval — run_eval.py

Bộ đánh giá chatbot RAG bất động sản (plan §10-11, §16.1). Chạy golden set qua
`eval/golden_set_v1.json` và bộ test anti-injection qua `eval/injection_test_vn.json`.

## Yêu cầu chạy thật

Verification thật cần 3 thứ ĐỀU SẴN: PostgreSQL (schema + seed), LLM (extract/answer/judge),
reranker. **Hiện tại chưa có infra** — chỉ chạy được `--dry`.

## CLI

```bash
python eval/run_eval.py                          # full golden set, backend thật
python eval/run_eval.py --subset 10              # 10 câu đầu
python eval/run_eval.py --only-category legal    # chỉ 1 category
python eval/run_eval.py --dry                    # CI: mock pipeline + mock judge (định thức)
python eval/run_eval.py --inject                 # chạy eval/injection_test_vn.json qua guard
python eval/run_eval.py --json-out eval/results.json
python eval/run_eval.py --fail-fast              # exit 1 nếu có câu fail
```

Flag: `--golden` (mặc định `eval/golden_set_v1.json`), `--subset N`, `--only-category`,
`--dry`, `--inject`, `--json-out PATH`, `--fail-fast`. Env: `POSTGRES_*`, `LLM_BASE_URL`,
`LLM_API_KEY`, `EVAL_JUDGE_MODEL` (ghim `deepseek-v4-flash-0731`), `EVAL_PIPELINE_TIMEOUT_S`.

## Thresholds (ngưỡng §11)

| Metric | Ngưỡng | Ghi chú |
|---|---|---|
| Numeric exact-match | **≥ 0.95** | Gate cứng — exit code 1 nếu thấp hơn (cuối `amain`) |
| Faithfulness (unsupported-claim) | **= 0** | Judge LLM: claim ngoài context → fail câu |
| Latency | **P50 < 6s, P95 < 10s** | In ở SUMMARY là PASS/CHECK (chưa phải exit gate) |
| Injection | **≥ 90%** | `_meta.ok_threshold_pct` trong `injection_test_vn.json` |

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

`eval/injection_test_vn.json`: **20 prompt tiếng Việt = 10 injection + 10 benign control.**
Mỗi prompt có `{id, prompt, label: injection|benign, expect_reject}`. Contract: injection
phải bị chặn (`expect_reject=true`), benign KHÔNG được reject (`expect_reject=false`).
Chạy bằng `--inject`; ghi FP/FN khi chạy. Pass khi `pass_pct >= ok_threshold_pct` (90).

## ⚠️ `--dry` — chỉ là harness self-test, KHÔNG phải verification pipeline

`--dry` dùng **`MockPipeline` + `MockJudge`**: trả payload khớp expectation của golden câu
để test harness đo được (định thức, không gọi PG/LLM/rerank). **Kết quả `--dry` KHÔNG chứng
minh pipeline thật đúng** — chỉ chứng minh harness chạy và đo được. Verification pipeline thật
cần chạy real run với PostgreSQL + LLM + rerank (hiện blocked: chưa có infra).