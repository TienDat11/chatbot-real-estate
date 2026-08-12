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