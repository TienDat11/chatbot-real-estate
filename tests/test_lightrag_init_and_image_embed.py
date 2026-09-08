"""Focused tests for lightrag_init embedding-key wiring (Defect 2) and the
image_search query embedder float encoding_format (Defect 3)."""

from __future__ import annotations

import asyncio

import pytest


def _fake_secret(tag: str) -> str:
    """Computed stand-in for provider keys in settings; never a real credential."""
    return "unit-" + tag + "-" + str(len(tag))


# --- Defect 2: lightrag_init binding=openrouter receives the key from env ---


@pytest.fixture
def _no_real_lightrag(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BINDING", "dashscope")


def _set_settings(monkeypatch, **overrides):
    from ingest import lightrag_init

    real = lightrag_init.settings
    for key, value in overrides.items():
        monkeypatch.setattr(real, key, value, raising=False)
    return lightrag_init


class _FakeEmbeddings:
    def __init__(self, recorder):
        self._recorder = recorder

    async def create(self, **kwargs):
        self._recorder.append(kwargs)

        class _D:
            index = 0
            embedding = [0.0] * 1024

        class _Resp:
            data = [_D()]

        return _Resp()


def test_lightrag_openrouter_binding_receives_key(monkeypatch):
    init = _set_settings(
        monkeypatch,
        embedding_binding="openrouter",
        openrouter_api_key=_fake_secret("or"),
        embedding_api_key="",
        openrouter_base_url="https://openrouter.ai/api/v1",
        embedding_openrouter_model="openai/text-embedding-3-small",
    )
    recorded: list[dict] = []

    import numpy as np
    import openai

    def _fake_async_openai(**kwargs):
        assert kwargs["api_key"] == _fake_secret("or")
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"

        class _C:
            embeddings = _FakeEmbeddings(recorded)

        return _C()

    monkeypatch.setattr(openai, "AsyncOpenAI", _fake_async_openai)
    func = init._make_embedding_func()
    assert func.embedding_dim == init.settings.embedding_dim
    arr = asyncio.run(func.func(["t"]))
    assert isinstance(arr, __import__("numpy").ndarray)
    assert arr.dtype == np.float32
    assert recorded[0]["encoding_format"] == "float"


def test_lightrag_openrouter_binding_falls_back_to_embedding_key(monkeypatch):
    init = _set_settings(
        monkeypatch,
        embedding_binding="openrouter",
        openrouter_api_key="",
        embedding_api_key=_fake_secret("emb"),
    )
    seen: dict = {}

    import openai

    def _fake_async_openai(**kwargs):
        seen.update(kwargs)

        class _C:
            embeddings = _FakeEmbeddings([])

        return _C()

    monkeypatch.setattr(openai, "AsyncOpenAI", _fake_async_openai)
    init._make_embedding_func()
    assert seen["api_key"] == _fake_secret("emb")


def test_lightrag_openrouter_binding_fails_closed_without_any_key(monkeypatch):
    init = _set_settings(
        monkeypatch,
        embedding_binding="openrouter",
        openrouter_api_key="",
        embedding_api_key="",
    )
    with pytest.raises(init.LightRAGUnavailableError, match="fail closed"):
        init._make_embedding_func()


# --- Defect 3: image_search embeds with explicit float encoding_format ---


def test_image_search_embed_query_uses_float_format_and_shared_config(monkeypatch):
    import api.application.services.image_search as img

    monkeypatch.setattr(
        img.settings, "embedding_binding", "openrouter", raising=False
    )
    monkeypatch.setattr(
        img.settings, "openrouter_api_key", _fake_secret("or"), raising=False
    )
    monkeypatch.setattr(
        img.settings,
        "openrouter_base_url",
        "https://openrouter.ai/api/v1",
        raising=False,
    )
    monkeypatch.setattr(
        img.settings,
        "embedding_openrouter_model",
        "openai/text-embedding-3-small",
        raising=False,
    )
    monkeypatch.setattr(img.settings, "embedding_dim", 1024, raising=False)

    recorded: list[dict] = []

    import openai

    class _D:
        index = 0
        embedding = [0.5] * 1024

    class _Resp:
        data = [_D()]

    class _Embeddings:
        async def create(self, **kwargs):
            recorded.append(kwargs)
            return _Resp()

    class _Client:
        embeddings = _Embeddings()

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: _Client())
    vector = asyncio.run(img._embed_query("mặt bằng"))
    assert len(vector) == 1024
    call = recorded[0]
    assert call["encoding_format"] == "float"
    assert call["input"] == ["mặt bằng"]
    assert "b64" not in str(call.get("encoding_format", "")).lower()


def test_image_search_embed_query_dim_mismatch_fails(monkeypatch):
    import api.application.services.image_search as img

    monkeypatch.setattr(
        img.settings, "embedding_binding", "dashscope", raising=False
    )
    monkeypatch.setattr(img.settings, "embedding_dim", 1024, raising=False)

    import openai

    class _D:
        index = 0
        embedding = [0.5] * 512  # wrong dim

    class _Resp:
        data = [_D()]

    class _Embeddings:
        async def create(self, **kwargs):
            return _Resp()

    class _Client:
        embeddings = _Embeddings()

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: _Client())
    with pytest.raises(ValueError, match="dim drift"):
        asyncio.run(img._embed_query("q"))


# --- Soleil KG retry: re-ingest cleanup must delete the stale extraction cache ---


class _FakeRagForDelete:
    """Records adelete calls; skips initialize_storages via the ready flag."""

    _re_storages_ready = True

    def __init__(self):
        self.calls: list[tuple[str, bool]] = []

    async def adelete_by_doc_id(self, doc_id, delete_llm_cache=False):
        self.calls.append((doc_id, delete_llm_cache))
        return {"status": "success"}


def test_adelete_by_doc_id_deletes_llm_cache_by_default():
    """Default delete_llm_cache=True: the LLM response cache is keyed by prompt
    content hash, so identical re-ingested text replays a cached (possibly
    empty) extraction forever if the flag stays at LightRAG's False default —
    the mechanism behind Soleil qd6608's 0-entity reprocessing."""
    from ingest.lightrag_init import adelete_by_doc_id

    rag = _FakeRagForDelete()
    asyncio.run(adelete_by_doc_id(rag, "legal-soleil-qd6608-2016:4:2"))
    assert rag.calls == [("legal-soleil-qd6608-2016:4:2", True)]


def test_adelete_by_doc_id_cache_delete_is_opt_out():
    from ingest.lightrag_init import adelete_by_doc_id

    rag = _FakeRagForDelete()
    asyncio.run(adelete_by_doc_id(rag, "doc:1:0", delete_llm_cache=False))
    assert rag.calls == [("doc:1:0", False)]
