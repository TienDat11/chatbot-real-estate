"""Unit + integration tests for POST /llms-hello (non-stream variant).

Drives the endpoint coroutine directly with a faked LLM and faked image/video
attachments so no network or DB is touched. Covers the response contract, the
happy path, and the LLM-timeout -> static-greeting degrade where imagery/videos
still attach.
"""

from __future__ import annotations

import asyncio

from api.application.services.project_config import render_template
from api.interfaces.api import hello as hello_mod


def run(coro):
    """Drive one coroutine on a fresh loop (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


class _FakeLLM:
    def __init__(self, text="Xin chào Anh/Chị ạ"):
        self._text = text

    async def complete(self, messages, **kwargs):
        return self._text


def _image(i):
    return {
        "image_id": f"img-{i}",
        "kind": "matbang",
        "title": "t",
        "url_cdn": "u",
        "score": 1.0,
        "match": "semantic",
        "reason": None,
    }


def _video(i):
    return {
        "title": f"v{i}",
        "kind": "brand",
        "url_cdn": "u",
        "poster_url": "p",
        "width": 1,
        "height": 1,
        "duration": None,
        "bytes_mb": None,
    }


def test_hello_response_fields_and_defaults():
    fields = set(hello_mod.HelloResponse.model_fields)
    assert fields == {"greeting", "trace_id", "images", "videos"}

    resp = hello_mod.HelloResponse(greeting="x", trace_id="t")
    assert resp.images == []
    assert resp.videos == []


def test_llms_hello_happy_path_attaches_images_and_videos(monkeypatch):
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào Anh/Chị ạ"))

    async def fake_images(top_k=6, kind="matbang"):
        return [_image(i) for i in range(4)]

    def fake_videos(project_key="camellia"):
        # list_project_videos is a frozen, synchronous config read.
        return [_video(i) for i in range(3)]

    monkeypatch.setattr(hello_mod, "search_project_images", fake_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fake_videos)

    resp = run(hello_mod.llms_hello())

    assert resp.greeting == "Chào Anh/Chị ạ"
    assert resp.trace_id.startswith("t-")
    assert len(resp.images) == 4
    assert len(resp.videos) == 3


def test_llms_hello_degrades_to_fallback_on_llm_timeout(monkeypatch):
    class _TimeoutLLM:
        async def complete(self, messages, **kwargs):
            raise asyncio.TimeoutError("gateway stuck")

    monkeypatch.setattr(hello_mod, "get_llm", lambda: _TimeoutLLM())

    async def fake_images(top_k=6, kind="matbang"):
        return [_image(0)]

    def fake_videos(project_key="camellia"):
        return [_video(0)]

    monkeypatch.setattr(hello_mod, "search_project_images", fake_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fake_videos)

    resp = run(hello_mod.llms_hello())

    assert resp.greeting == render_template(hello_mod._FALLBACK_GREETING)
    assert len(resp.images) == 1
    assert len(resp.videos) == 1


def test_llms_hello_images_empty_when_search_degrades(monkeypatch):
    # search_project_images is best-effort and returns [] on DB/embed failure;
    # the greeting must still succeed with an empty image list.
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào ạ"))

    async def empty_images(top_k=6, kind="matbang"):
        return []

    def fake_videos(project_key="camellia"):
        return [_video(0)]

    monkeypatch.setattr(hello_mod, "search_project_images", empty_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fake_videos)

    resp = run(hello_mod.llms_hello())

    assert resp.greeting == "Chào ạ"
    assert resp.images == []
    assert len(resp.videos) == 1
