"""Story 10.2 — de-hardcode Camellia across prompts, media, intent, and config.

Proves the answer path renders identity from the project registry instead of
carrying a hardcoded Camellia literal: a non-camellia project_key must produce
prompts/directives/greetings with that project's name and none of the default
identity, while None keeps the legacy Camellia behavior (regression).
"""

from __future__ import annotations

from api.application.services import project_config as pc
from api.application.services.conv_state import conv_directive
from api.application.services.generate import build_messages, _SYSTEM_PROMPT
from api.application.services.merge import Merged
from api.application.services.sales_kit import sales_kit_block
from api.domain.services.route_intent import Intent, classify_intent

# Mirrors db/seed/project_config.sql identity fields for the second project.
# All fields are strings: render_template substitutes every registry field and
# str.replace would raise TypeError on a None replacement value.
_SOLEIL_IDENTITY = {
    "ten_thuong_mai": "The Soleil Đà Nẵng",
    "ten_phap_ly": "Tổ hợp Ánh Dương - Soleil",
    "vi_tri": "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
    "hotline": "0905 555 555",
}


def _patch_identity(monkeypatch, identity: dict | None) -> None:
    """Point the registry read at a canned identity (no DB in unit tests)."""
    monkeypatch.setattr(pc, "fetch_project_identity", lambda key=None: dict(identity or pc._CAMELLIA_IDENTITY))


def _merged(query: str, project_key: str | None) -> Merged:
    return Merged(
        rag_blocks="RAG_CONTEXT\nđoạn văn bản pháp luật...",
        evidence_blocks="FACT_EVIDENCE\n[fe-001] giá 2.1 tỷ",
        sources=[],
        facts=[],
        meta={"query": query, "rewritten": query, "project_key": project_key},
    )


# --- registry render helper --------------------------------------------------

def test_render_template_replaces_placeholders_for_soleil(monkeypatch):
    _patch_identity(monkeypatch, _SOLEIL_IDENTITY)
    out = pc.render_template(
        "Dự án {ten_thuong_mai} tại {vi_tri} ({project})", "soleil"
    )
    assert "The Soleil Đà Nẵng" in out
    assert "Phạm Văn Đồng" in out
    assert "soleil" in out
    assert "Camellia" not in out


def test_render_template_defaults_to_camellia(monkeypatch):
    _patch_identity(monkeypatch, None)
    out = pc.render_template("Dự án {ten_thuong_mai}", None)
    assert "The Camellia Son Tra - Da Nang" in out


def test_render_template_unknown_placeholder_left_untouched(monkeypatch):
    _patch_identity(monkeypatch, _SOLEIL_IDENTITY)
    out = pc.render_template("Còn {unknown} giữ nguyên", "soleil")
    assert "{unknown}" in out


def test_brand_token_derives_from_project_name(monkeypatch):
    _patch_identity(monkeypatch, _SOLEIL_IDENTITY)
    assert pc.brand_token("soleil") == "soleil"


def test_brand_token_defaults_to_camellia(monkeypatch):
    _patch_identity(monkeypatch, None)
    assert pc.brand_token(None) == "camellia"


# --- system_policy.md + sales_kit_vn.md (prompt assets) ---------------------

def test_system_prompt_template_has_no_hardcoded_project():
    assert "The Camellia" not in _SYSTEM_PROMPT
    assert "{ten_thuong_mai}" in _SYSTEM_PROMPT
    assert "{vi_tri}" in _SYSTEM_PROMPT


def test_sales_kit_title_renders_per_project(monkeypatch):
    _patch_identity(monkeypatch, _SOLEIL_IDENTITY)
    block = sales_kit_block("soleil")
    assert "The Soleil Đà Nẵng" in block
    assert "Camellia" not in block


def test_sales_kit_title_defaults_to_camellia(monkeypatch):
    _patch_identity(monkeypatch, None)
    block = sales_kit_block()
    assert "The Camellia" in block


# --- build_messages (answer generation path) --------------------------------

def test_build_messages_system_prompt_renders_soleil_identity(monkeypatch):
    _patch_identity(monkeypatch, _SOLEIL_IDENTITY)
    msgs = build_messages(_merged("giá căn 2PN", "soleil"), None)
    system = next(m for m in msgs if m["role"] == "system")
    assert "The Soleil Đà Nẵng" in system["content"]
    assert "Camellia" not in system["content"]


def test_build_messages_system_prompt_defaults_to_camellia(monkeypatch):
    _patch_identity(monkeypatch, None)
    msgs = build_messages(_merged("giá căn 2PN", None), None)
    system = next(m for m in msgs if m["role"] == "system")
    assert "The Camellia Son Tra - Da Nang" in system["content"]


# --- conv directives ---------------------------------------------------------

def test_conv_directive_names_the_active_project(monkeypatch):
    _patch_identity(monkeypatch, _SOLEIL_IDENTITY)
    greet = conv_directive("greet", "soleil")
    assert "The Soleil Đà Nẵng" in greet
    assert "Camellia" not in greet
    qualify = conv_directive("qualify", "soleil")
    assert "The Soleil Đà Nẵng" in qualify


def test_conv_directive_defaults_to_camellia(monkeypatch):
    _patch_identity(monkeypatch, None)
    greet = conv_directive("greet")
    assert "The Camellia Son Tra - Da Nang" in greet


# --- intent classification keyword ------------------------------------------

def test_company_intent_uses_project_brand_token():
    # "soleil là của ai" only matches via the per-project brand token keyword
    # ("{token} là của ai") — the generic _COMPANY_KEYWORDS list has no "của ai".
    assert classify_intent(
        "soleil là của ai", project_name="soleil"
    ).intent == Intent.COMPANY
    assert classify_intent(
        "camellia là của ai", project_name="camellia"
    ).intent == Intent.COMPANY
    # Legacy default token: a caller that passes no project_name still classifies
    # the Camellia ownership phrase as COMPANY (unchanged pre-10.2 behavior).
    assert classify_intent("camellia là của ai").intent == Intent.COMPANY
    # Without the matching brand token the same phrase is NOT a company question.
    assert classify_intent("soleil là của ai").intent != Intent.COMPANY


# --- default-project constant (media call sites) ----------------------------

def test_default_project_key_is_camellia():
    assert pc.DEFAULT_PROJECT_KEY == "camellia"
