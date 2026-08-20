# HF-0 Smoke — Prompt Loading restored after DDD layer refactor

> Epic 4 · Day 0 · Date 2026-08 · Story: hf-0-prompt-load-hotfix
> Verdict: **PASS** — system policy + rewrite few-shot load from the canonical api/prompts/ dir; LightRAG 1.5.6 entity profile contract restored.

## What broke (R1)

The DDD layer refactor moved modules into api/application/, api/domain/, api/infrastructure/
but left stale parents[1] path constants, so prompt assets were looked up at the wrong depth:

| File | Before (bug) | Resolves to | Effect |
|---|---|---|---|
| api/application/services/generate.py | parents[1] / prompts/system_policy.md | api/application/prompts/ | _SYSTEM_PROMPT silently fell back to 226-char default (persona + hard rules lost) |
| api/domain/services/rewrite.py | parents[1] / prompts/rewrite_fewshot.md | api/domain/prompts/ | _FEWSHOT empty -> router lost few-shot examples |
| api/infrastructure/config/config.py | _REPO_ROOT = parents[1] | api/infrastructure/ | .env never loaded from repo root (LLM/embedding/rerank config invisible) |
| api/application/services/rag_leg.py | entity_type_prompt_file=prompts/entity_type/legal_vn.yml | — | LightRAG 1.5.6 rejects paths with separators (bare filename only) |

Root prompts/ also sat outside the code tree — ambiguous ownership vs the canonical
api/prompts/ the plan defines.

## Changes (HF-0)

1. **Moved 3 prompt assets** (git mv, history preserved) into canonical api/prompts/:
   - api/prompts/system_policy.md (2209 chars — v1; story 4.2 rewrites to v2 >3000)
   - api/prompts/rewrite_fewshot.md (3757 chars)
   - api/prompts/entity_type/legal_vn.yml — **re-written to LightRAG 1.5.6 schema**
     (was legacy ENTITY_TYPES list; now entity_types_guidance + 3 text examples +
     1 json example; validated in both text and json mode via validate_entity_extraction_prompt_profile_for_mode)
   - api/prompts/__init__.py empty package marker; root prompts/ removed.
2. **Fixed 2 path constants**: parents[1] -> parents[2] in generate.py and rewrite.py
   (both under api/<layer>/services/ -> parents[2] = api/ -> api/prompts/).
3. **Fixed config.py _REPO_ROOT**: parents[1] -> parents[3] (= repo root) so
   pydantic-settings loads .env / .env.<APP_ENV> correctly. Verified: llm_base_url/key now populated.
4. **Added PROMPT_DIR config**: Settings.prompt_dir (default absolute <repo>/api/prompts)
   exported by export_runtime_env() -> LightRAG resolves entity_type/<file> under the new dir.
   Documented in .env.example.
5. **Restored eval/run_eval.py** from source: dest copy had a stale indentation bug
   (_NUM_TOKEN/_VND_UNIT scoped inside a dangling block -> NameError in extract_amounts),
   blocking the daily eval --dry PASS gate. Diff was exactly this one hunk.
6. **Regression test** tests/test_prompt_assets.py (6 tests): assert prompt load from file,
   canonical dir layout, LightRAG 1.5.6 yml contract (bare filename, new schema), and PROMPT_DIR export.

## Verification (all green)

| Gate | Command | Result |
|---|---|---|
| compile | python -m compileall -q api ingest eval | OK |
| tests | python -m pytest tests/ -q | **148 passed** (142 baseline + 6 new) |
| import-smoke | python -c import api.application.services.generate | system_prompt loaded: True \| len: 2209 · fewshot loaded: True \| len: 3757 |
| eval dry | python eval/run_eval.py --dry | **34/34 ALL PASS** (content/numeric/routing/refusal/freshness/faithfulness 100%) |

## Notes / next

- Story 4.2 rewrites system_policy.md to v2 (longer) — the test threshold len > 1000 still holds; plan's >3000 becomes valid then.
- Story 4.3 (LightRAG integration) can rely on legal_vn.yml already being in the 1.5.6-gated format (spike 4 output — verified here, no separate spike needed).
- ingest/lightrag_init.py does not pass entity_type_prompt_file at construction (uses default profile); query-time QueryParam passes it — unchanged behavior, now against a valid file.
