"""Focused tests: LLM structured-output capability fallback (Defect 1).

Covers: fallback triggers only on the 400 unsupported-structured-outputs
signature, the per-(base_url, model) capability cache prevents repeat retries,
lenient JSON parsing (code fences / prose), and normal models are unaffected.
"""

from __future__ import annotations

import pytest

from api.infrastructure.adapters.openai_compatible_llm import (
    OpenAICompatibleLLM,
    _no_structured_output,
    parse_json_leniently,
)


class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResp:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, behaviors: list):
        self.behaviors = list(behaviors)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return _FakeResp(behavior)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


def _make_adapter(monkeypatch, behaviors) -> tuple[OpenAICompatibleLLM, _FakeCompletions]:
    completions = _FakeCompletions(behaviors)
    adapter = OpenAICompatibleLLM(api_key="k", base_url="https://gw.example/v1")
    monkeypatch.setattr(adapter._client, "chat", _FakeChat(completions), raising=False)
    return adapter, completions


@pytest.fixture(autouse=True)
def _clear_cache():
    _no_structured_output.clear()
    yield
    _no_structured_output.clear()


class _ProviderError(Exception):
    """Mimics the openai SDK APIStatusError surface (carries status_code)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _unsupported_400() -> Exception:
    return _ProviderError(
        'Error code: 400 - INVALID_REQUEST_BODY: model "inclusionai/ling-3.0-flash-fin:free" '
        "does not support feature: structured-outputs",
        400,
    )


@pytest.mark.asyncio
async def test_fallback_retries_once_without_response_format(monkeypatch):
    adapter, completions = _make_adapter(
        monkeypatch, [_unsupported_400(), '{"ok": true}']
    )
    out = await adapter.complete([{"role": "user", "content": "json please"}], json_mode=True)
    assert out == '{"ok": true}'
    assert len(completions.calls) == 2
    assert completions.calls[0].get("response_format") == {"type": "json_object"}
    assert "response_format" not in completions.calls[1]
    assert ("https://gw.example/v1", adapter.default_model) in _no_structured_output


@pytest.mark.asyncio
async def test_capability_cache_skips_response_format_on_next_call(monkeypatch):
    adapter, completions = _make_adapter(
        monkeypatch, [_unsupported_400(), '{"a": 1}', '{"b": 2}']
    )
    await adapter.complete([{"role": "user", "content": "x"}], json_mode=True)
    await adapter.complete([{"role": "user", "content": "y"}], json_mode=True)
    assert len(completions.calls) == 3
    assert "response_format" not in completions.calls[2]


@pytest.mark.asyncio
async def test_other_400_never_retried_without_response_format(monkeypatch):
    adapter, completions = _make_adapter(
        monkeypatch, [_ProviderError("400 bad request: oops", 400)]
    )
    with pytest.raises(Exception, match="oops"):
        await adapter.complete([{"role": "user", "content": "x"}], json_mode=True)
    assert len(completions.calls) == 1
    assert _no_structured_output == set()


@pytest.mark.asyncio
async def test_500_not_treated_as_capability_gap(monkeypatch):
    adapter, completions = _make_adapter(
        monkeypatch, [_ProviderError("500 server error", 500)]
    )
    with pytest.raises(Exception):
        await adapter.complete([{"role": "user", "content": "x"}], json_mode=True)
    assert len(completions.calls) == 1
    assert _no_structured_output == set()


@pytest.mark.asyncio
async def test_normal_model_with_structured_outputs_unaffected(monkeypatch):
    adapter, completions = _make_adapter(monkeypatch, ['{"ok": true}', '{"ok2": true}'])
    first = await adapter.complete([{"role": "user", "content": "x"}], json_mode=True)
    second = await adapter.complete([{"role": "user", "content": "x"}], json_mode=True)
    assert first == '{"ok": true}' and second == '{"ok2": true}'
    assert len(completions.calls) == 2
    assert all(c.get("response_format") == {"type": "json_object"} for c in completions.calls)
    assert _no_structured_output == set()


@pytest.mark.asyncio
async def test_json_mode_not_requested_keeps_response_format_absent(monkeypatch):
    adapter, completions = _make_adapter(monkeypatch, ["plain text"])
    await adapter.complete([{"role": "user", "content": "x"}], json_mode=False)
    assert "response_format" not in completions.calls[0]


def test_parse_json_leniently_strips_code_fence():
    assert parse_json_leniently('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_leniently_extracts_first_object_from_prose():
    assert parse_json_leniently('Here you go: {"a": {"b": 2}} hope it helps') == {"a": {"b": 2}}


def test_parse_json_leniently_plain_object():
    assert parse_json_leniently('{"x": [1, 2]}') == {"x": [1, 2]}


def test_parse_json_leniently_raises_when_no_json():
    from api.infrastructure.adapters.openai_compatible_llm import LLMError

    with pytest.raises(LLMError):
        parse_json_leniently("no json at all")
    with pytest.raises(LLMError):
        parse_json_leniently("")
