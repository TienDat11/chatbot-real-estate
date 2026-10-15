r"""Raw-LaTeX guard for user-facing Vietnamese answers.
The answer LLM occasionally emits LaTeX (\(...\), \frac{a}{b}, $$...$$) that the
chat UI renders as markup. The prompt bans it (system_policy.md §GIỌNG VĂN) and
``normalize_answer_display`` rewrites a COMPLETED answer to readable Unicode —
non-destructively (delimiters/commands only, unknown markup left verbatim, no
text dropped). The per-token dash rule in ``sanitize_output`` is unchanged;
normalization runs only on the joined answer. LGN-P0-002 changed delivery, not
normalization: no token frame leaves ``generate``, and ``output_guard`` emits the
normalized answer once verification passes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from api import workflow as workflow_module
from api.domain.services.guard_output import normalize_answer_display
from api.workflow import RagQueryWorkflow
from api.application.services import merge as merge_module
from api.application.services.project_context import ProjectContext
from api.application.services.rag_leg import RagLegResult


_PROJECT_CONTEXT = ProjectContext("camellia", "normal", (("camellia", "Camellia"),))
_AS_OF = date(2026, 9, 29)
_EVIDENCE_CONTENT = "Camellia diện tích 68 m2"


# --- prompt policy -------------------------------------------------------------


def test_system_policy_bans_raw_latex():
    from api.application.services.generate import _SYSTEM_PROMPT

    assert "LaTeX" in _SYSTEM_PROMPT
    assert "KHÔNG dùng LaTeX thô" in _SYSTEM_PROMPT


# --- normalize_answer_display ---------------------------------------------------


def test_delimiters_stripped_delimited_and_display_style():
    assert normalize_answer_display(r"\(x = 5\)") == "x = 5"
    assert normalize_answer_display(r"\[x = 5\]") == "x = 5"
    assert normalize_answer_display("$$x = 5$$") == "x = 5"
    assert normalize_answer_display(r"giá \(4\) tỷ đồng") == "giá 4 tỷ đồng"


def test_frac_to_plain_slash():
    assert normalize_answer_display(r"\frac{a}{b}") == "a/b"
    assert normalize_answer_display(r"\frac {68}{2} m2") == "68/2 m2"


def test_known_commands_map_to_unicode():
    assert normalize_answer_display(r"68m2 \times 3{,}5") == "68m2 × 3{,}5"
    assert normalize_answer_display(r"DT \leq 100 \geq 50 \neq 0") == "DT ≤ 100 ≥ 50 ≠ 0"


def test_text_command_unwraps_content():
    assert normalize_answer_display(r"\text{VNĐ} 2 tỷ") == "VNĐ 2 tỷ"


def test_unknown_commands_left_verbatim():
    assert normalize_answer_display(r"\alpha \beta") == r"\alpha \beta"


def test_stray_braces_and_content_preserved():
    r"""Unbalanced/stray braces are untouched, and no character is ever dropped."""
    assert normalize_answer_display(r"\frac{a}{") == r"\frac{a}{"  # unbalanced pair left verbatim
    assert normalize_answer_display("a {b} c") == "a {b} c"
    assert normalize_answer_display("x^{2} + 1") == "x^{2} + 1"  # superscripts untouched


def test_empty_and_none_safe():
    assert normalize_answer_display("") == ""
    assert normalize_answer_display(None) == ""


# --- workflow boundary ----------------------------------------------------------


class _FakeReranker:
    async def rerank(self, query, chunks):
        return chunks


def _make_routed(rewritten: str):
    from api.domain.services.rewrite import RoutedResult

    return RoutedResult(
        rewritten=rewritten,
        routing={
            "needs_rag": True,
            "needs_sql": False,
            "structured_path": "none",
            "needs_geo": False,
        },
        sql_spec=None,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=False,
        as_of=_AS_OF,
    )


def _run_workflow(
    monkeypatch, tokens: list[str], *, rewritten: str = "diện tích 68 m2"
) -> tuple[dict, list]:
    """Harness mirrors tests/test_workflow_images.py: real workflow, every
    external call patched; ``fake_stream`` yields the given raw tokens.

    One scoped, published, in-date Camellia chunk backs the prompt so the
    output guard can verify the answer and the LGN-P0-002 delivery contract is
    exercised on a turn that would otherwise fail closed for lack of evidence."""
    events: list[tuple[str, dict]] = []

    async def fake_guard(raw):
        from api.domain.services.guard_input import GuardResult as InputGuardResult

        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed(rewritten)

    async def fake_stream(merged, history, high_stakes):
        marker = "RAG_CONTEXT (chunks từ LightRAG, đã lọc hiệu lực + rerank):\n"
        merged.meta["_prompt_rag_rows"] = json.loads(
            merged.rag_blocks.split(marker, 1)[1].split("\n", 1)[0]
        )
        merged.meta["_prompt_fact_rows"] = json.loads(merged.evidence_blocks.split("\n", 3)[2])
        for t in tokens:
            yield t

    async def fake_output_guard(answer, facts, sources, routing, meta=None):
        from api.domain.services.guard_output import GuardResult as OutputGuardResult

        return OutputGuardResult(confidence="MEDIUM", requires_review=False, verdicts={})

    async def fake_audit(entry):
        return None

    async def fake_rag(*args):
        return RagLegResult(
            chunks=[{"id": "camellia-area", "content": _EVIDENCE_CONTENT, "score": 0.9}]
        )

    async def fake_sql(*args):
        return workflow_module.SqlLegResult([], {"mode": "none"}, degraded=False)

    class Connection:
        async def fetch(self, sql_text, ids):
            return [{
                "chunk_id": "camellia-area", "registry_content": _EVIDENCE_CONTENT,
                "doc_id": "camellia-doc", "title": "Hồ sơ tiện ích Camellia",
                "section": "Tiện ích", "kind": "project", "project_key": "camellia",
                "status": "published", "effective_from": _AS_OF - timedelta(days=1),
                "effective_to": None,
            }]

    class Acquire:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *exc): return None

    class Pool:
        def acquire(self): return Acquire()

    async def fake_pool(): return Pool()

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", fake_stream)
    monkeypatch.setattr(workflow_module, "guard_output", fake_output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)
    monkeypatch.setattr(workflow_module, "run_rag_leg", fake_rag)
    monkeypatch.setattr(workflow_module, "run_sql_leg", fake_sql)
    monkeypatch.setattr(
        workflow_module, "search_images", lambda *a, **k: asyncio.sleep(0, result=[])
    )
    monkeypatch.setattr(merge_module, "get_ro_pool", fake_pool)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(
            query="q",
            session_id=None,
            history=[],
            project_context=_PROJECT_CONTEXT,
        )

    return asyncio.run(go()), events


def test_sse_tokens_verified_then_delivered_once_normalized(monkeypatch):
    """LGN-P0-002 contract: no token may leave the pipeline before output_guard
    verifies the answer; the single delivered token frame is the normalized
    answer, and no raw LaTeX (no \\( \\) or \\frac) ever reaches the customer.

    The raw-latex chunks are fed only through the buffer, not streamed, so the
    customer cannot observe LaTeX glyphs — the concatenated token stream and the
    done payload answer are the same normalized text, delivered exactly once."""
    tokens = [r"diện tích \(", "68", r" m2\), \frac{a}{b}"]
    result, events = _run_workflow(monkeypatch, tokens)

    token_events = [d for e, d in events if e == workflow_module.SSE_EVENT_TOKEN]
    # Post-verification contract: exactly one token frame carrying the full
    # normalized answer (LGN-P0-002 verify-before-delivery).
    assert len(token_events) == 1
    delivered = token_events[0]["text"]
    assert delivered == "diện tích 68 m2, a/b"
    assert "\\" not in delivered  # no raw LaTeX delimiters/commands reach the user

    # Stream/done equivalence: the single token equals the done payload answer.
    assert result["answer"] == delivered
    assert result["answer"] == normalize_answer_display("".join(tokens))
