"""Story 4.6 — Model 2-tier selection matrix (§7.2) + max_tokens propagation.

`select_answer_tier` decides pro vs flash from conv_state / high_stakes /
lead_cta_hint; `stream_answer` resolves the model + max_tokens and forwards
them to the LLM adapter. No LLM/DB — the adapter (api.infrastructure.dependencies.llm)
is monkeypatched.
"""

from api.application.services.generate import select_answer_tier, stream_answer
from api.application.services.merge import Merged


def _merged(**meta) -> Merged:
    return Merged(rag_blocks="", evidence_blocks="", sources=[], facts=[], meta=meta)


# --- selection matrix (pure) ---


def test_greet_is_flash():
    m = _merged(conv_state="greet")
    assert select_answer_tier(m, high_stakes=False) == "flash"


def test_handoff_done_is_flash():
    m = _merged(conv_state="handoff_done")
    assert select_answer_tier(m, high_stakes=False) == "flash"


def test_qualify_is_pro():
    m = _merged(conv_state="qualify")
    assert select_answer_tier(m, high_stakes=False) == "pro"


def test_recommend_is_pro():
    m = _merged(conv_state="recommend")
    assert select_answer_tier(m, high_stakes=False) == "pro"


def test_nurture_is_pro():
    m = _merged(conv_state="nurture")
    assert select_answer_tier(m, high_stakes=False) == "pro"


def test_no_conv_state_defaults_flash():
    m = _merged()
    assert select_answer_tier(m, high_stakes=False) == "flash"


def test_high_stakes_is_pro_always():
    m = _merged(conv_state="greet")
    assert select_answer_tier(m, high_stakes=True) == "pro"


def test_lead_cta_hint_at_greet_is_flash():
    # A CTA hint only ever fires inside a conversion state, so a bare greet with
    # a stray hint must not force pro (single signal = CONVERSION_STATES).
    m = _merged(conv_state="greet", lead_cta_hint="Anh/chị để lại số nhé")
    assert select_answer_tier(m, high_stakes=False) == "flash"


# --- stream_answer: model + max_tokens propagation (mock adapter) ---


def _collect(merged, high_stakes):
    """Run stream_answer collecting tokens, mocking the llm adapter (sync wrapper)."""
    import asyncio

    import api.application.services.generate as gen

    calls = {}

    async def fake_stream(*args, **kwargs):
        calls["model"] = kwargs.get("model")
        calls["max_tokens"] = kwargs.get("max_tokens")
        yield "Xin chào"

    async def _run():
        out = [t async for t in stream_answer(merged, [], high_stakes)]
        return "".join(out)

    orig = gen.llm
    gen.llm = type("Fake", (), {"stream": fake_stream})()
    try:
        text = asyncio.run(_run())
    finally:
        gen.llm = orig
    return text, calls


def test_stream_answer_pro_tier_forces_max_tokens_6000():
    m = _merged(conv_state="recommend")
    text, calls = _collect(m, False)
    assert text == "Xin chào"
    assert calls["max_tokens"] == 6000


def test_stream_answer_flash_tier_uses_4000():
    m = _merged(conv_state="greet")
    text, calls = _collect(m, False)
    assert calls["max_tokens"] == 4000


def test_stream_answer_sets_tier_in_meta():
    m = _merged(conv_state="nurture")
    _, _ = _collect(m, False)
    assert m.meta["answer_tier"] == "pro"
