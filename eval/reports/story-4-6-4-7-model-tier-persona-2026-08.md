# Story 4.6 + 4.7 — Model 2-tier + Persona eval gate (report)

> Epic 4 · Date 2026-08-18 · Branch: feat/story-4-6-4-7-model-tier-persona
> Verdict: **PASS** — selection matrix + max_tokens wired; persona golden 15/15 (dry).

## Story 4.6 — Model 2-tier theo trạng thái hội thoại (R4)

- **ports/llm.py + openai_compatible_llm.py**: thêm `max_tokens` vào `stream()` (complete() đã có từ 4.5).
- **generate.py**: `select_answer_tier(merged, high_stakes)` — pro khi conv_state ∈ {qualify, recommend, nurture}
  HOẶC lead_cta_hint HOẶC high_stakes; ngược lại flash. `stream_answer` chọn model theo tier
  (llm_model_answer_pro / llm_model_answer), set max_tokens (pro 6000, flash 4000), ghi `answer_tier` vào meta (audit).
- **workflow.py**: merge step đưa `conv_state` vào merged.meta (làm tier selector signal).
- **constants.py**: note §7.3 — DEFAULT_MODEL_ANSWER_PRO cần verify bằng 1 call thật trước khi đổi
  (tránh 400 mọi lượt conversion); override env LLM_MODEL_ANSWER_PRO.

### Tests (tests/test_model_tier.py, 11 passed)
- Matrix: greet→flash, handoff_done→flash, qualify/recommend/nurture→pro, no-state→flash,
  high_stakes→pro, lead_cta_hint→pro.
- stream_answer: pro→max_tokens 6000, flash→4000, meta answer_tier=pro.

## Story 4.7 — Persona eval + acceptance gate cho Epic 5

- **eval/golden_persona_v1.json**: 15 case (5 objection, 4 affordability, 3 legal, 2 greeting, 1 refusal),
  mỗi case expect {has_direct_answer, has_citation, next_step_questions, no_em_dash, no_robot_phrase,
  disclosure_type, cta_allowed}. 1/3 case có history 2 turn.
- **eval/run_eval.py --persona**: regex/structure check (KHÔNG cần LLM-judge để dry PASS); in từng case + rule vi phạm.
- **eval/scripts/persona_smoke.py**: 5 hội thoại 3-turn thật (cần keys/PG), ghi eval/reports/persona-YYYY-MM.md.

### Verification
| Gate | Result |
|---|---|
| compileall api eval | OK |
| pytest (model_tier + synonyms + rewrite + route + conv + sales_kit + prompt_assets + persona_prompt + guard + price + workflow) | 209 passed |
| eval/run_eval.py --dry | ALL PASS |
| eval/run_eval.py --persona --dry | 15/15 PASS |

## Gate ghi nhận
- Persona 15/15 (hoặc 14/15 + waiver) là điều kiện done của 5.4 FE integration (ghi trong epics.md deps).
- Live smoke 5 hội thoại + live persona cần quota AI Box — chạy khi quota hồi (persona_smoke.py + run_eval --persona).
