import pytest
from pydantic import ValidationError

from api.infrastructure.config.config import Settings


def _settings_with(**overrides) -> Settings:
    # Build a minimal Settings instance: the secret validators only bite in
    # prod, and dev defaults keep every other field valid.
    return Settings(app_env="dev", **overrides)


def test_rag_query_mode_default_is_hybrid():
    # Default behavior unchanged: hybrid stays the pinned production mode.
    assert _settings_with().rag_query_mode == "hybrid"


def test_rag_query_mode_env_override_is_accepted(monkeypatch):
    monkeypatch.setenv("RAG_QUERY_MODE", "mix")
    assert _settings_with().rag_query_mode == "mix"


def test_rag_query_mode_is_normalized(monkeypatch):
    monkeypatch.setenv("RAG_QUERY_MODE", "  LOCAL ")
    assert _settings_with().rag_query_mode == "local"


@pytest.mark.parametrize("bad", ["bogus", "", "HYBRIDS"])
def test_rag_query_mode_invalid_value_fails_closed(monkeypatch, bad):
    # Fail at settings load (not first query) — a typo'd mode must never boot
    # the API into a silently different retrieval path.
    monkeypatch.setenv("RAG_QUERY_MODE", bad)
    with pytest.raises(ValidationError):
        _settings_with()


def test_make_query_param_reads_settings_mode(monkeypatch):
    from unittest.mock import patch

    from api.domain.value_objects.constants import (
        DEFAULT_MAX_ENTITY_TOKENS,
        DEFAULT_MAX_RELATION_TOKENS,
        DEFAULT_MAX_TOTAL_TOKENS,
    )
    from api.application.services import rag_leg

    captured: dict = {}

    class FakeParam:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    fake_module = type("M", (), {"QueryParam": FakeParam})()
    monkeypatch.setenv("RAG_QUERY_MODE", "local")

    def fake_cfg(key, default=None):
        # Only the mode key is overridden; the token-budget keys keep real
        # settings values so _get_rag_budget stays on its normal path.
        if key == "rag_query_mode":
            return "local"
        return default

    with patch.object(rag_leg, "get_cfg", fake_cfg):
        with patch.dict("sys.modules", {"lightrag.lightrag": fake_module}):
            rag_leg._make_query_param(["hl"], ["ll"])

    assert captured["mode"] == "local"
    assert captured["only_need_context"] is True
    assert captured["enable_rerank"] is False
    assert captured["max_entity_tokens"] == int(DEFAULT_MAX_ENTITY_TOKENS)
