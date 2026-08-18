# Story 4.5 — Conversation Engine: ConvContext State Machine + Qualification + CTA Policy Report

**Date:** 2026-08-18  
**Story ID:** 4.5 (absorbs old 4.2 conv shell + old 4.3 SSE routing)  
**Status:** COMPLETED  
**Commit:** `feat(story-4-5): conversation engine conv_state lru + qualification slots + sse routing + cta policy`

---

## 1. Objectives & Scope Achieved

1. **ConvContext LRU & State Machine (§6.2)**:
   - In-memory `ConvContext` dataclass tracking `session_id`, `state` (greet, qualify, recommend, nurture, handoff_done), `slots`, `useful_turns`, `last_cta_turn`, `cta_shown_count`, `interested_units`, `turn`, `updated_at`.
   - `_LRU` cache bounded to `SESSIONS_MAX = 512`, `TTL_SECONDS = 7200` (2 hours).
   - State transitions mapped strictly to §6.2 transition matrix:
     - `greet` -> `qualify` on new slot / purchase intent.
     - `greet` / `qualify` / `nurture` -> `recommend` on affordability answered.
     - `recommend` -> `nurture` on 3 useful turns without phone.
     - `handoff_done` is terminal.
     - Side-tracks (`LEGAL`, `OTHER`, `LOCATION`, `CLOSURE` [FIX-7]) never mutate funnel state.
   - `mark_phone_given` sets `phone_given=True` and transitions to `handoff_done`.

2. **Deterministic & Fail-Open LLM Slot Extraction (§6.3)**:
   - Deterministic extractors:
     - `budget_vnd`: reuses `price_calc.extract_budget`
     - `bedrooms`: regex matching studio, 1PN, 2PN, 3PN
     - `view`: keyword detection (view biển, view hồ bơi, view hồ, view núi, view sông, hồ bơi, biển, công viên, nội khu)
     - `timeline`: keywords ("gấp", "tháng này", "cuối năm", "sau tết", "năm sau")
     - `purpose`: "stay" ("ở thật", "an cư", "để ở", "tự ở") vs "invest" ("đầu tư", "cho thuê", "kinh doanh")
   - LLM slot extraction: 1 JSON call (model extract `qwen3.7-flash`, timeout 8s) triggered only when query has >= 6 words and unfilled slots remain; fail-open on any exception.
   - `lead_prefill_note` formats prefill notes (e.g. `Ngân sách: 4 tỷ · Quan tâm: 2PN · View: view biển`).

3. **CONVERSATION_DIRECTIVE Dynamic Injection (§6.4)**:
   - Per-state directives defined in `conv_state.py`:
     - `greet`: Chào ấm 1 câu + trả lời + 1 câu hỏi mở về nhu cầu.
     - `qualify`: Trả lời + hỏi đúng 1 slot còn thiếu (budget -> bedrooms -> timeline -> purpose).
     - `recommend`: Trả lời + so sánh tối đa 3 căn từ evidence + 1 dòng mời tư vấn.
     - `nurture`: Recap 2 giá trị khách quan tâm + 1 lời mời nhận cuộc gọi 5 phút.
     - `handoff_done`: Xác nhận chuyên viên sẽ gọi trong ~5 phút, không hỏi thêm.
   - Dynamically injected as a `system` message in `generate.py::build_messages`.

4. **CTA Policy (§6.5)**:
   - `maybe_lead_cta_hint` gates on all 5 conditions:
     - (a) `useful_turns >= 1`
     - (b) `phone_given` is False
     - (c) `turn - last_cta_turn >= 2`
     - (d) current answer not `requires_review`
     - (e) `cta_shown_count < 3` per session
   - Rotates through 3 standardized variants (`CTA_VARIANTS`).

5. **SSE Routing Event Wiring across 3 Layers (§6.6)**:
   - `packages/contracts/src/constants.ts`: Added `API_SSE_EVENTS.ROUTING = "routing"` + added to `SSE_EVENT_NAMES`.
   - `packages/contracts/src/index.ts`: Exported `SseRoutingPayload` and updated `SseEventName`.
   - `apps/web/src/lib/api.ts`: Added `onRouting` handler and switch-case for `API_SSE_EVENTS.ROUTING`.
   - `api/interfaces/api/main.py`: Wired `/query` via `RagQueryPipelineConv` which emits `routing` before legs.

6. **Hotfix: Config Shadowing Repair**:
   - Fixed pre-existing latent bug across `generate.py`, `sql_leg.py`, `rag_leg.py`, `guard_input.py`, `workflow.py` where `get_settings as get_cfg` shadowed the 2-argument `api.get_cfg` helper.

---

## 2. Verification Gate Results

| Gate | Target | Result | Status |
|---|---|---|---|
| `compileall` | Clean compilation on `api`, `eval`, `ingest` | All clean | PASS |
| `pytest tests/` | All unit & integration tests pass | **224 / 224 PASS** (49 new conv tests) | PASS |
| `eval/run_eval.py --dry` | 34/34 golden eval scenarios pass | **34 / 34 PASS (100.0%)** | PASS |
| Regression Checks | Sales kit, persona prompt, guard, FIX-7 | Zero regressions | PASS |
