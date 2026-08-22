"""Hello stream endpoint tests.

POST /llms-hello/stream must emit token events then a done event with trace_id,
and degrade to the static greeting (streamed) when the LLM is unavailable.
Frame-parsing only: uses the pure generator, not a live LLM.
"""

import asyncio
import json

from api.application.services.project_config import render_template
from api.interfaces.api.hello import _frame, _sanitize_greeting, _FALLBACK_GREETING


def test_sanitize_greeting_removes_dashes():
    out = _sanitize_greeting("Xin chào \u2014 Anh/Chị \u2013 đã quan tâm")
    assert "\u2014" not in out
    assert "\u2013" not in out
    assert "Xin chào - Anh/Chị - đã quan tâm" == out


def test_frame_shape():
    frame = _frame("token", {"text": "chào"})
    assert frame == 'event: token\ndata: {"text": "chào"}\n\n'


def test_fallback_greeting_is_dash_safe():
    greeting = render_template(_FALLBACK_GREETING)
    assert "\u2014" not in greeting
    assert "\u2013" not in greeting
    assert "tiện ích nổi bật" in greeting
    assert "{ten_thuong_mai}" not in greeting  # placeholder must render


def test_stream_emits_tokens_then_done():
    """Drive _stream_greeting with a fake LLM returning fixed tokens."""
    from api.interfaces.api import hello as hello_mod

    class FakeLLM:
        async def stream(self, messages, **kwargs):
            for tok in ("Chào ", "Anh/Chị", " ạ"):
                yield tok

    original = hello_mod.get_llm
    hello_mod.get_llm = lambda: FakeLLM()

    async def collect():
        return [frame async for frame in hello_mod._stream_greeting("s1", None)]

    try:
        frames = asyncio.run(collect())
    finally:
        hello_mod.get_llm = original

    events = [f.split("\n")[0].replace("event: ", "") for f in frames]
    assert events[0] == "token"
    assert events[-1] == "done"
    joined = "".join(
        json.loads(f.split("\ndata: ", 1)[1])["text"]
        for f in frames
        if f.startswith("event: token")
    )
    assert joined == "Chào Anh/Chị ạ"
