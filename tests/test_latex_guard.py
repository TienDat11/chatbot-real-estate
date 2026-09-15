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

from api import workflow as workflow_module
from api.domain.services.guard_output import normalize_answer_display
from api.workflow import RagQueryWorkflow


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
            "needs_rag": False,
            "needs_sql": False,
            "structured_path": "none",
            "needs_geo": False,
        },
        sql_spec=None,
        hl_keywords=[],
        ll_keywords=[],
        high_stakes=False,
        as_of=None,
    )


def _run_workflow(monkeypatch, tokens: list[str]) -> tuple[dict, list]:
    """Harness mirrors tests/test_workflow_images.py: real workflow, every
    external call patched; ``fake_stream`` yields the given raw tokens."""
    events: list[tuple[str, dict]] = []

    async def fake_guard(raw):
        from api.domain.services.guard_input import GuardResult as InputGuardResult

        return InputGuardResult(clean=raw, rejected=False, degraded=True)

    async def fake_rewrite(clean, history, as_of_iso):
        return _make_routed(rewritten)

    async def fake_stream(merged, history, high_stakes):
        for t in tokens:
            yield t

    async def fake_output_guard(answer, facts, sources, routing, meta=None):
        from api.domain.services.guard_output import GuardResult as OutputGuardResult

        return OutputGuardResult(confidence="MEDIUM", requires_review=False, verdicts={})

    async def fake_audit(entry):
        return None

    monkeypatch.setattr(workflow_module, "guard_input", fake_guard)
    monkeypatch.setattr(workflow_module, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(workflow_module, "get_reranker", lambda: _FakeReranker())
    monkeypatch.setattr(workflow_module, "stream_answer", fake_stream)
    monkeypatch.setattr(workflow_module, "guard_output", fake_output_guard)
    monkeypatch.setattr(workflow_module, "write_audit", fake_audit)

    async def go():
        wf = RagQueryWorkflow(on_event=lambda e, d: events.append((e, d)))
        return await wf.run(query="q", session_id=None, history=[])

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
