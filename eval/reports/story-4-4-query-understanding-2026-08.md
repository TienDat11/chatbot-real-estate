# Story 4.4 — Query understanding v2: khẩu ngữ + synonyms (R7) — Smoke report

> Epic 4 · Date 2026-08 · Commit: feat(story-4-4): query understanding v2 colloquial synonyms + fewshot
> Verdict: **PASS** — 10 câu khẩu ngữ thật chạy qua deterministic pipeline (classify_intent +
> enrich_hl_keywords + extract_slots_deterministic) cho routing/keywords/slots đúng kỳ vọng.
> Live LLM rewrite (rewrite_query) cần keys/PG nên chạy ở 4.7 persona eval gate (như 4.3 đã ghi).

## 10 câu khẩu ngữ thật (người 45-70) — đã verify 2026-08

Bảng dưới là output THẬT của pipeline deterministic (không phỏng đoán). `intent` là
classify_intent (Story 4.1); `kw` là enrich_hl_keywords (Story 4.4, ADD-only); `slots` là
extract_slots_deterministic (Story 4.5). `other` = fall-through cho LLM rewrite router (đúng
thiết kế — không phải short-circuit).

| # | Câu khẩu ngữ | intent | hl_keywords (enrich) | slots | Ghi chú |
|---|---|---|---|---|---|
| 1 | "nhà 2 ngủ buzview biển còn không con" | other | 2PN, view biển | view=view biển | typo "buzview" + khẩu ngữ → fewshot Ex6 |
| 2 | "tụi tui có 3 tỉ thôi mua được gì hông" | other | (rỗng) | budget_vnd=3.000.000.000 | affordability → fewshot Ex7 |
| 3 | "trả góp 0% là sao, có bị lừa hông" | price | HTLS, 0% | (rỗng) | financing KHÔNG high-stakes → Ex8 |
| 4 | "tầng mấy đẹp nhất giá tốt" | price | (rỗng) | (rỗng) | pricing tier → fewshot Ex9 |
| 5 | "căn góc có view biển không bà con" | other | view biển, căn góc | view=view biển | 2 synonym cùng bắn |
| 6 | "cho hỏi cọc bao nhiêu là giữ được căn" | price | tiền cọc | (rỗng) | "cọc" → tiền cọc |
| 7 | "bên mình có hỗ trợ trả chậm không" | other | HTLS, 0% | (rỗng) | "trả chậm" → HTLS |
| 8 | "tui muốn mua để cho thuê, căn nào hợp" | other | (rỗng) | purpose=invest | slot purpose |
| 9 | "đóng sớm 95% được giảm bao nhiêu" | other | sớm 95 | (rỗng) | "đóng sớm" → sớm 95 |
| 10 | "bàn giao khi nào vậy em" | other | bàn giao | (rỗng) | "bàn giao" → bàn giao |

## Mapping fewshot (rewrite_fewshot.md, Example 6-9)

- Ex6: "nhà 2 ngủ buzview biển còn không con" → routing pricing/spec + hl view biển.
- Ex7: "tụi tui có 3 tỉ thôi mua được gì hông" → affordability (budget 3e9) + rewritten tự chứa.
- Ex8: "trả góp 0% là sao, có bị lừa hông" → needs_rag + hl HTLS (KHÔNG high-stakes).
- Ex9: "tầng mấy đẹp nhất giá tốt" → pricing tier.

## Verification

| Gate | Result |
|---|---|
| compileall api | OK |
| pytest tests/test_synonyms.py | 14 passed |
| pytest tests/test_rewrite.py tests/test_route_intent.py | 39 passed |
| import-smoke enrich | 'nhà 2 ngủ hướng biển' → ['2PN','view biển'] |

## Acceptance notes

- Enrichment ADD-only: không đè LLM output, dedup, case-insensitive (test assert).
- "trả góp 0%" KHÔNG bị gán high-stakes (test test_normalize_routed_enriches_htls_not_high_stakes).
- Live smoke 10 câu (rewrite LLM thật) chạy ở 4.7 persona eval gate — cần keys/PG, cùng lý do 4.3.
