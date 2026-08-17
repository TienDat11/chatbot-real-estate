"""HF-0 regression: prompt assets live in api/prompts/ and load at import.

The DDD layer refactor left stale ``parents[1]`` path constants pointing at
api/{application,domain} instead of api/, so generate.py silently fell back to
the 226-char default system prompt and rewrite.py lost its few-shot (R1). If a
prompt asset is moved or deleted, these tests go red.

Also pins the LightRAG 1.5.6 contract: entity_type_prompt_file must be a bare
file name (resolved under PROMPT_DIR/entity_type) and the YAML must use the
new schema (entity_types_guidance + entity_extraction_examples[/_json]).
"""

from pathlib import Path

from api.application.services.generate import _SYSTEM_PROMPT
from api.domain.services.rewrite import _FEWSHOT

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ASSETS = _REPO_ROOT / "api" / "prompts"


def test_system_policy_loaded_from_file():
    assert len(_SYSTEM_PROMPT) > 1000  # fallback default is 226 chars
    assert "QUY TẮC CỨNG" in _SYSTEM_PROMPT


def test_rewrite_fewshot_loaded_from_file():
    assert len(_FEWSHOT) > 1000
    assert "Example 1" in _FEWSHOT


def test_assets_live_in_canonical_dir():
    assert (_ASSETS / "system_policy.md").exists()
    assert (_ASSETS / "rewrite_fewshot.md").exists()
    assert (_ASSETS / "entity_type" / "legal_vn.yml").exists()


def test_entity_type_profile_uses_1506_contract():
    raw = (_ASSETS / "entity_type" / "legal_vn.yml").read_text(encoding="utf-8")
    assert "entity_types_guidance:" in raw
    assert "entity_extraction_examples:" in raw
    assert "entity_extraction_json_examples:" in raw
    assert "ENTITY_TYPES" not in raw  # legacy list format removed


def test_rag_leg_uses_bare_entity_filename():
    src = (_REPO_ROOT / "api" / "application" / "services" / "rag_leg.py").read_text(
        encoding="utf-8"
    )
    assert '"entity_type_prompt_file": "legal_vn.yml"' in src
    assert "prompts/entity_type" not in src  # LightRAG 1.5.6 forbids separators


def test_prompt_dir_exported_for_lightrag():
    import os

    from api.infrastructure.config.config import export_runtime_env, get_settings

    prior = os.environ.get("PROMPT_DIR")
    os.environ.pop("PROMPT_DIR", None)
    try:
        export_runtime_env()
        settings = get_settings()
        assert os.environ.get("PROMPT_DIR") == settings.prompt_dir
        assert settings.prompt_dir.replace("\\", "/").endswith("api/prompts")
    finally:
        if prior is not None:
            os.environ["PROMPT_DIR"] = prior
        else:
            os.environ.pop("PROMPT_DIR", None)
