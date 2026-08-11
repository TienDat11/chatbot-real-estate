# Seed data — rag-real-estate (bảng giá + chính sách vay + văn bản pháp luật)

> ⚠️ **MOCK/DEMO DATA** — dữ liệu demo được tạo để chạy pipeline, CHƯA phải dữ liệu thật từ
> chủ dữ liệu (plan §10: data owner sẽ cung cấp). Chưa từng được áp lên DB thật; KHÔNG dùng
> trong production.

> Plan: `.claude/plans/rag-real-estate-final.plan.md` §3.3, §3.4, §3.6, §10 (Ngày 3-4), §16.1
> Ngày: 2026-08-10 | Chạy TRƯỚC khi ingest (schema đã áp từ `db/schema.sql`).

## Các file

| File | Nội dung | Loại dữ liệu |
|---|---|---|
| `price_campaigns.sql` | 2 campaign giá (tower-a-2026q2 active, tower-b-2026q1 expired) + 2 documents dự án (project tower-a/b) + 2 documents bảng giá (price) + fact_subjects + facts: price_vnd + area_m2 cho 29 căn + policy vay (bank_a/bank_b mỗi căn, 1 policy support 0%, 1 căn KHÔNG policy) | `price` + `project` |
| `policy_vay.sql` | Ma trận chính sách vay chuẩn + INSERT policy facts (deposit_pct/term_months/interest_rate_pct) gắn campaign + policy_key — chạy độc lập hoặc song song `price_campaigns.sql` (an toàn: NOT EXISTS guard) | finance policy |
| `legal_docs.sql` | 10 văn bản pháp luật T4 (registry `documents` kind=legal) | `legal` |

## Thứ tự nạp

```bash
# sau khi đã áp db/schema.sql
psql "$POSTGRES_URL" -f db/seed/legal_docs.sql
psql "$POSTGRES_URL" -f db/seed/price_campaigns.sql
psql "$POSTGRES_URL" -f db/seed/policy_vay.sql   # optional — đã có policy trong price_campaigns.sql
# verify
psql "$POSTGRES_URL" -f scripts/verify_ingest.sql
psql "$POSTGRES_URL" -c "SELECT subject_id, policy_key, price_vnd, deposit_pct, term_months, interest_rate_pct, required_down_payment_vnd FROM v_unit_offers WHERE subject_id = (SELECT id FROM fact_subjects WHERE subject_key='unit:tower-a/A10-01') ORDER BY policy_key;"
```

## Kỷ luật kiểu số (AD-14)

- Tiền `NUMERIC(20,0)` — giá trị NGUYÊN đồng (8e9, không phải 8000e6).
- `%` điểm phần trăm `NUMERIC(5,2)` — `25.00` = 25% trả trước.
- Lãi suất `NUMERIC(6,4)` — `8.5000` = 8.5%/năm.
- Diện tích `NUMERIC(10,2)` — `85.50` m².
- **NULL ≠ 0.00**: policy 0% (support) là `deposit_pct = 0.00` hợp lệ; căn KHÔNG policy thì KHÔNG có row deposit/term → không xuất hiện trong `v_unit_offers` (inner join policy).

## 2 campaign (freshness + as-of)

| campaign_key | project | Interval | Trạng thái hôm nay (2026-08-10) |
|---|---|---|---|
| `tower-a-2026q2` | tower-a | [2026-04-01, NULL) | **ACTIVE** — flagship case "2 tỉ" nằm ở đây |
| `tower-b-2026q1` | tower-b | [2026-01-01, 2026-03-31) | **EXPIRED** — dùng test freshness (giá cũ KHÔNG hiện) + as_of (as_of=2026-02-15 → hiện giá cũ) |

> Lưu ý: document `price-tower-b-2026q1` giữ `status='published'` (đã công bố hợp lệ Q1/2026) còn
> **campaign** `tower-b-2026q1` đánh `status='expired'` + facts interval đóng `effective_to=2026-03-31`
> → RLS vẫn cho SELECT (doc published) nhưng `v_unit_offers` (chỉ facts hiện hành) loại bỏ → đúng
> semantics freshness §3.6. Nếu đánh doc `expired` thì RLS chặn luôn cả as_of → hỏng test lịch sử.

## Dữ liệu units

- **Tower A (21 căn, active)** — giá 1.2 tỷ → 8 tỷ:
  - 19 căn: 2 policy ngân hàng mỗi căn (`bank_a`: 25%/180 tháng/8.5%; `bank_b`: 30%/240 tháng/8.0%).
  - `A06-01`: CHỈ policy `support` — trả trước **0%** (deposit 0.00, term 120, lãi 0.00) → test "0% ≠ NULL".
  - `A04-03`: **KHÔNG policy** → không xuất hiện trong `v_unit_offers` (edge case §3.4).
- **Tower B (8 căn, expired)** — giá 5.3 tỷ → 9.5 tỷ, cùng 2 policy ngân hàng (interval đã đóng).

### Case chuẩn "có 2 tỉ" (§3.4, success signal 1)

`A10-01` giá `8.000.000.000` × bank_a `25.00%` → `required_down_payment_vnd = CEIL(8e9×25/100) = 2.000.000.000`
→ query `WHERE required_down_payment_vnd <= 2000000000` bao gồm căn này. Loan 6 tỷ, term 180,
monthly_principal ≈ 33.333.333.

## Content hash

`documents.content_hash` là **placeholder sha256 hex** của `seed:<doc_id>` (chưa phải hash file gốc).
Khi ingest chạy trên file thật, `ingest/load.py` sẽ thay bằng hash SHA-256 thực của file gốc (AD-7).

## Idempotent

Mọi INSERT dùng `ON CONFLICT (doc_id/campaign_key/subject_key) DO NOTHING`; facts dùng
`INSERT ... SELECT ... WHERE NOT EXISTS` (guard theo subject/fact_key/policy_key/effective_from).
Chạy lại nhiều lần không lỗi / không nhân bản. **Lưu ý**: `facts_no_overlap` (exclusion constraint)
sẽ CHẶN insert chồng interval — nếu cần thay đổi dữ liệu cũ, dùng `scripts/update_price.sh`
(expire + insert CÙNG 1 transaction), KHÔNG sửa row cũ.

## Chính sách vay (bank_a vs bank_b)

| policy_key | deposit_pct | term_months | interest_rate_pct | Ghi chú |
|---|---|---|---|---|
| `bank_a` | 25.00 | 180 | 8.5000 | Ngân hàng A |
| `bank_b` | 30.00 | 240 | 8.0000 | Ngân hàng B |
| `support` | 0.00 | 120 | 0.0000 | Gói hỗ trợ 0% trả trước — ÁP DỤNG cho A06-01 |

> Policy facts có `source_doc_id` trỏ vào document bảng giá của campaign (không có document policy
> riêng ở MVP) — RLS `facts_pub_select` yêu cầu doc published nên trỏ doc price là đủ.
