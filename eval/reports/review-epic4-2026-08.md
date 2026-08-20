# Code Review — Epic 4 (HF-0 + 4.2 + 4.3 + 4.5)

**Date:** 2026-08-18  
**Method:** /code-review (Standards + Spec axes) + /security-review + /bmad-review (adversarial, edge-case, verification-gap lenses) — run as parallel sub-agents.

## Scope split into 2 PRs
- **PR-A**: HF-0 prompt-load hotfix + Story 4.2 (sales voice system_policy v2 + disclosure) + Story 4.3 (sales_kit + SALES_CONTEXT inject + anti-drift) — commits 645fe8f..8c2853e.
- **PR-B**: Story 4.5 conversation engine (ConvContext LRU + slots + SSE routing + CTA policy) — commits 5ce0980..cda2f5a + review fixes.

---

## Findings by axis

### Standards (PR-A)
1. **Duplicated Code** — path resolution `Path(__file__).resolve().parents[2]` repeated in generate.py, rewrite.py, sales_kit.py. *Judgement call; config.prompt_dir exists as the canonical source.*
2. **Feature Envy / redundant compute** — build_messages re-runs classify_intent on raw query. *Fixed: now classifies from rewritten query.*
3. **Primitive Obsession** — guard_output returns untyped dict verdicts. *Judgement call.*

### Spec (PR-A)
1. Live 10-question / 5-question smoke tests deferred to Story 4.7 (reports note this). *Accepted — unit tests cover static invariants.*
2. **build_messages classifies intent from raw query not rewritten** — multi-turn colloquial queries could misclassify and skip SALES_CONTEXT. **FIXED** (generate.py now uses rewritten first).

### Standards (PR-B)
1. **Import-after-def in conv_slots.py** (E402) — **FIXED** (moved import to top).
2. **Lazy imports in workflow.py step methods** (PLC0415) — **FIXED** (moved conv_state import to module top).
3. **maybe_lead_cta_hint has side effects** during pre-run routing emission — if run_inner fails, state already mutated. *Judgement call / accepted (routing is emitted before legs by design §6.6).*
4. **lead_prefill_note :.0f truncation** (3.5 tỷ → 4 tỷ) — **FIXED** (decimal formatting).
5. **Duplicated keyword tuples** between conv_state.py and conv_slots.py — *judgement call; noted for 4.4 consolidation.*

### Spec (PR-B)
1. **/api/lead endpoint + mark_phone_given wiring absent** — deferred to Story 6.3 per plan (mark_phone_given implemented + tested; endpoint is Epic 6 scope). *Accepted/deferred, documented.*
2. History FE + ConvContext merge partial — only current query + ctx.slots used, not full history text. *Accepted for MVP.*
3. **CTA review-gating uses previous turn's status** (routing emitted before legs) — inherent §6.6 constraint. *Accepted, documented.*

### Security
1. **high_stakes source robustness** — guard_output reads routing dict; **FIXED** defensively (also checks meta + nested).
2. History screening assumes string content — *low; noted.*
3. Prompt-injection boundary well-gated (L2 hierarchy, SALES_CONTEXT in data block, rule 4). *No action.*
4. Secrets: .env ignored, config validates prod secrets. *No action.*

### bmad — Edge-Case lens
1. **Bedroom regex false positives** ("12PN"→2PN, "2phans"→2pn) — **FIXED** (word boundaries).
2. **get_context(None) shared cache key** — **FIXED** (monotonic anon key per caller).
3. **Purpose overlap** ("mua để ở, không đầu tư" → invest) — **FIXED** (negation-aware, stay-first).
4. **lead_prefill_note negative/0 budget** — **FIXED** (guarded, omitted).
5. CTA count mutated before client ack — *accepted (SSE best-effort).*
6. LRU eviction edge at exactly SESSIONS_MAX — *accepted (put() evicts on > max).*
7. LLM slot-fill type validation — *noted for 4.4/4.6.*

### bmad — Verification-Gap lens
1. **RagRgreConvWorkflow / RagQueryPipelineConv never exercised by tests** (the classes behind /query) — **FIXED** (added 5 async integration tests with stubbed inner workflow).
2. **SSE routing event contract untested end-to-end** — **FIXED** (integration test asserts routing emitted before done).
3. **CONVERSATION_DIRECTIVE pipeline injection untested** (only build_messages unit) — *partially addressed by integration tests.*
4. **Weak LRU bound assertion** (<=512) — **FIXED** (now asserts ==512 + eviction order).
5. **CTA multi-turn persistence untested** — *covered by unit gates; integration added.*
6. **mark_phone_given unwired in API** — deferred to 6.3.

---

## Fixes applied (all gates green: 233 tests, compileall OK, eval --dry 34/34 PASS)
1. generate.py — classify intent from rewritten query (Spec PR-A).
2. conv_slots.py — bedroom regex word boundaries; purpose negation; budget decimal format; import relocation.
3. conv_state.py — get_context None/empty anon key guard.
4. workflow.py — top-level conv_state import (removed lazy import).
5. guard_output.py — defensive high_stakes resolution.
6. tests/test_conv_workflow.py — +9 tests (5 integration + 4 edge-case regressions); strengthened LRU bound.
