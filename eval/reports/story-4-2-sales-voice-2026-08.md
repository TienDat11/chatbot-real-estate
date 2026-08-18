# Story 4.2 — Sales voice: system prompt v2 + contextual disclosure

> Epic 4 · Date 2026-08 · Commit: feat(story-4-2): sales voice system prompt + disclosure
> Verdict: **PASS** — v2 sales-voice system prompt loads (4756 chars); contextual
> disclosure + robot-phrase checks added to the L4 output guard; all gates green.

## What changed

1. **api/prompts/system_policy.md rewritten to v2** (plan §3.1, 6 sections):
   - Persona: chuyên viên tư vấn cao cấp của dự án The Camellia Sơn Trà, Đà Nẵng.
   - QUY TẮC CỨNG: rules 1-8 kept verbatim (evidence-only / no self-calc / citation /
     no data-command / cầm cố distinction / refusal / money format / Vietnamese).
   - New: KIẾN TRÚC CÂU TRẢ LỜI (4 layers), GIỌNG VĂN (Anh/Chị + em; no robot phrases;
     no em-dash; 80-180 words), DISCLOSURE THEO NGỮ CẢNH (replaces the cold always-on
     disclaimer: price/estimate → 'định hướng'; high-stakes → chuyên viên; greeting → none),
     CONVERSATION_DIRECTIVE block (priority over default layer 4).
2. **generate.py**: stamps merged.meta['prompt_version'] = 'v2' beside prompt_hash.
3. **guard_output.py** (plan §3.3.2):
   - _contextual_disclosure_verdict(): price/estimate answers must carry a disclosure
     keyword ('định hướng', 'bảng hàng chính thức', 'chưa xác nhận chính thức',
     'ước lượng'); high-stakes must steer to 'chuyên viên'; normal answers need none
     (FE footer owns the always-on line).
   - _robot_phrase_verdict(): bans robot clusters ('Dựa trên thông tin được cung cấp',
     'Như đã nêu ở trên', 'Theo yêu cầu của bạn', 'Tôi là AI/trợ lý ảo',
     'Hy vọng thông tin hữu ích') + em-dash.
   - Both are style_warn flags — never lower confidence (stays numeric/citation
     grounding per the plan invariant).
4. **Tests**: tests/test_persona_prompt.py (structure, 8 hard-rule key phrases, no-em-dash
   samples, directive block, len > 3000) + tests/test_guard_output.py (6 contextual
   disclosure cases + robot phrase/em-dash + full-pipeline verdict presence).

## Verification

| Gate | Result |
|---|---|
| compileall api ingest eval | OK |
| pytest | **165 passed** (148 → 165, +17 new) |
| import-smoke | v2 loaded: True | len: 4756 (fallback 226 can never substitute) |
| eval --dry | **34/34 ALL PASS** |

## Acceptance notes

- Import-smoke: prompt v2 load (len > 3000) — PASS.
- Live 10-question smoke (5 price/afford, 3 legal, 2 greeting) needs real LLM infra + DB;
  it runs under the story 4.7 persona eval gate. The L4 deterministic checks already
  enforce no-em-dash / no-robot / disclosure-by-context on the unit side.
- Cold AI-disclaimer line removed from the prompt (FE footer owns it; FE contract in story 5.4/5.5).