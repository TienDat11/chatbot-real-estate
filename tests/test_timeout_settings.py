"""Timeout settings contract — slot-fill LLM and rag_leg aquery_data budgets.

Both timeouts moved from hard-coded literals to env-tunable Settings fields
(defaults raised for the slow Google Gemini OpenAI-compatible route). The
declared defaults are asserted straight off the pydantic model so a developer's
host .env cannot flip them; env override is asserted through a fresh Settings
(instance env outranks dotenv in pydantic-settings), clearing the get_settings
cache on both sides.
"""

from __future__ import annotations

import pytest

from api.infrastructure.config.config import Settings, get_settings

_EXPECTED_DEFAULTS = {
    "llm_slot_fill_timeout_s": 30.0,  # slot-fill LLM completion budget
    "rag_aquery_timeout_s": 180.0,  # one rag_leg aquery_data attempt
}


def test_timeout_field_defaults_are_raised() -> None:
    for field_name, expected in _EXPECTED_DEFAULTS.items():
        default = Settings.model_fields[field_name].default
        assert default == expected, f"{field_name} default drifted"


def test_timeout_fields_honor_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_SLOT_FILL_TIMEOUT_S", "45")
    monkeypatch.setenv("RAG_AQUERY_TIMEOUT_S", "240")
    get_settings.cache_clear()
    try:
        cfg = get_settings()
        assert cfg.llm_slot_fill_timeout_s == 45.0
        assert cfg.rag_aquery_timeout_s == 240.0
    finally:
        get_settings.cache_clear()
