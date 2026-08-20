# Story 4.3 — Sales kit v1: tri thức bán hàng + objection playbook (R5)

> Epic 4 · Date 2026-08 · Commit: feat(story-4-3): sales kit v1 + anti-drift
> Verdict: **PASS** — kit SALES_KIT_V1 (~1146 tokens ≤ 1200) loaded;
> conditional SALES_CONTEXT injection wired into the generate data block.

## What changed

1. **api/prompts/sales_kit_vn.md (new)** — sales kit v1 per plan §4.1:
   - A. USP 6 dòng (vị trí, MBLand, 469+10, Q1/2028, cơ cấu 81/82/20+186/84/16, pháp lý).
   - B. Benefit-translation 10 dòng, mỗi dòng ghi nguồn (unit_catalog/project_info/...).
   - C. Payment selling angles (cọc 100tr, booking 50tr, CK 4%/13%/5% + EB 3%,
     HTLS 0% 18 tháng vay 70%, vốn tự có).
   - D. Objection playbook 8 mục (giá cao, sợ pháp lý, suy nghĩ thêm, vốn ít,
     dự án khác, tầng 4/13/14, đầu tư cho thuê, lo tiến độ) — phản hồi → fact → KHÔNG nói.
   - E. FOMO template (chỉ khi fact có khoảng hiệu lực).
   - Tail pending_confirm: hotline, lat/lng, tuổi vay, giá CH-10/11, Zalo OA.
   - Mọi số có nguồn (file.json → field; gt = feedback_data.txt).
2. **api/application/services/sales_kit.py (new)** — loader: SALES_KIT_VERSION,
   sales_kit_block() với delimiter; inject_sales_context() (PRICE/COMPANY/HANDOFF → True).
3. **generate.py** — build_messages chèn SALES_CONTEXT vào data block khi intent
   price/payment/project/handoff (classify_intent Story 4.1); legal/off-topic không chèn
   (test assert data block sạch).
4. **tests/test_sales_kit.py (new)** — version/delimiter, token budget ≤1200, sections,
   inject condition (7 intents), message-shape inject/skip (price/handoff → có;
   legal/off-topic → không), anti-drift (mọi số/%/ngày tồn tại trong nguồn đã dẫn).

## Verification

| Gate | Result |
|---|---|
| compileall api ingest eval | OK |
| pytest | 175 passed (165 + 10 mới ở test_sales_kit.py) |
| import-smoke | sales kit loaded: SALES_KIT_V1 | len ~4010 |
| eval --dry | 34/34 ALL PASS |

## Acceptance notes

- Kit ≤ 1200 token (~1146) PASS; anti-drift PASS (mọi số có nguồn).
- '30% vốn tự có HTLS (M5)' không có nguồn literal → viết lại nguồn-anchored
  ('vay tối đa 70%, còn lại vốn tự có + phí') thay vì tự bịa — đúng tinh thần plan.
- Câu legal thuần KHÔNG có SALES_CONTEXT trong data block (test assert).
- Smoke 5 câu objection cần live LLM (chạy ở 4.7 persona eval gate).
