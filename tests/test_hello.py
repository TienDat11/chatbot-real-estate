"""Unit + integration tests for POST /llms-hello (non-stream variant).

Drives the endpoint coroutine directly with a faked LLM and faked image/video
attachments so no network or DB is touched. Covers the response contract, the
happy path, and the LLM-timeout -> static-greeting degrade where imagery/videos
still attach.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request

from api.interfaces.api import hello as hello_mod


def run(coro):
    """Drive one coroutine on a fresh loop (no pytest-asyncio dependency)."""
    return asyncio.run(coro)


def request():
    """Build the request object required by the current greeting handler."""
    return Request({"type": "http", "method": "GET", "path": "/llms-hello", "headers": []})


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
    assert {"greeting", "trace_id", "images", "videos"}.issubset(fields)
    assert {"audience", "suggestions"}.issubset(fields)

    resp = hello_mod.HelloResponse(greeting="x", trace_id="t")
    assert resp.audience == "customer"
    assert resp.suggestions == []
    assert resp.images == []
    assert resp.videos == []


def test_llms_hello_happy_path_attaches_images_and_videos(monkeypatch):
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào Anh/Chị ạ"))

    async def fake_images(top_k=6, kind="matbang", project_key=None):
        return [_image(i) for i in range(4)]

    def fake_videos(project_key="camellia"):
        # list_project_videos is a frozen, synchronous config read.
        return [_video(i) for i in range(3)]

    monkeypatch.setattr(hello_mod, "search_project_images", fake_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fake_videos)

    resp = run(hello_mod.llms_hello(request()))

    assert resp.greeting == "Chào Anh/Chị ạ"
    assert resp.trace_id.startswith("t-")
    assert len(resp.images) == 4
    assert len(resp.videos) == 3


def test_llms_hello_degrades_to_fallback_on_llm_timeout(monkeypatch):
    class _TimeoutLLM:
        async def complete(self, messages, **kwargs):
            raise asyncio.TimeoutError("gateway stuck")

    monkeypatch.setattr(hello_mod, "get_llm", lambda: _TimeoutLLM())

    async def fake_images(top_k=6, kind="matbang", project_key=None):
        return [_image(0)]

    def fake_videos(project_key="camellia"):
        return [_video(0)]

    monkeypatch.setattr(hello_mod, "search_project_images", fake_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fake_videos)

    resp = run(hello_mod.llms_hello(request()))

    assert resp.greeting == hello_mod._audience_content(None, "customer")[0]
    assert resp.audience == "customer"
    assert len(resp.images) == 1
    assert len(resp.videos) == 1


def test_llms_hello_total_deadline_retains_resolved_media(monkeypatch):
    # FR-34: even when the total deadline cuts the LLM off, the media resolved
    # BEFORE the LLM phase must ride along with the static greeting.
    monkeypatch.setenv("HELLO_DEADLINE_S", "0.02")

    class _SlowLLM:
        async def complete(self, messages, **kwargs):
            await asyncio.sleep(0.2)
            return "should not be returned"

    monkeypatch.setattr(hello_mod, "get_llm", lambda: _SlowLLM())

    async def fast_images(top_k=6, kind="matbang", project_key=None):
        return [_image(i) for i in range(2)]

    def fast_videos(project_key="camellia"):
        return [_video(i) for i in range(3)]

    monkeypatch.setattr(hello_mod, "search_project_images", fast_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fast_videos)

    response = run(hello_mod.llms_hello(request()))

    assert response.greeting == hello_mod._audience_content(None, "customer")[0]
    assert response.trace_id.startswith("t-")
    assert response.audience == "customer"
    assert len(response.images) == 2
    assert len(response.videos) == 3


def test_llms_hello_media_budget_timeout_keeps_videos_and_llm_greeting(monkeypatch):
    # AC5(a): a media-budget timeout degrades images to [] but the cheap sync
    # video list is kept, and the LLM greeting itself still wins the remaining
    # total deadline.
    monkeypatch.setenv("HELLO_MEDIA_BUDGET_S", "0.01")

    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào Anh/Chị ạ"))

    async def slow_images(top_k=6, kind="matbang", project_key=None):
        await asyncio.sleep(0.2)
        return [_image(0)]

    def fast_videos(project_key="camellia"):
        return [_video(i) for i in range(2)]

    monkeypatch.setattr(hello_mod, "search_project_images", slow_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fast_videos)

    resp = run(hello_mod.llms_hello(request()))

    assert resp.greeting == "Chào Anh/Chị ạ"
    assert resp.images == []
    assert len(resp.videos) == 2


def test_llms_hello_static_greeting_short_and_dash_free():
    # AC5(c): the customer static greeting stays brief, sales-oriented, and
    # free of the display-forbidden em/en dashes for every project key.
    for key in (None, "camellia", "soleil"):
        text = hello_mod._audience_content(key, "customer")[0]
        assert len(text) <= 300
        assert "\u2014" not in text
        assert "\u2013" not in text
        assert "số điện thoại" in text  # sales-oriented: invites the 1-1 consult
        assert "em chào Anh/Chị" in text  # audience marker contract


def test_llms_hello_no_media_still_greets(monkeypatch):
    # Both media sources empty: the greeting succeeds with images=[] but the
    # (empty here) video list is still attached per the media-first contract.
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào ạ"))

    async def empty_images(top_k=6, kind="matbang", project_key=None):
        return []

    async def empty_recent(project_key, limit=8):
        return []

    monkeypatch.setattr(hello_mod, "search_project_images", empty_images)
    monkeypatch.setattr(hello_mod, "fetch_recent_project_images", empty_recent)
    monkeypatch.setattr(hello_mod, "list_project_videos", lambda *a, **k: [])

    resp = run(hello_mod.llms_hello(request()))

    assert resp.greeting == "Chào ạ"
    assert resp.images == []
    assert resp.videos == []


def test_llms_hello_scope_failure_skips_media_fetch(monkeypatch):
    # A project-scope failure must resolve no project media at all (no
    # cross-project leak): images stay [] even though the default media
    # fakes would return rows; only the default video list is read.
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào ạ"))

    from api.application.services.project_scope import ProjectScopeError

    async def raise_scope(project_key):
        raise ProjectScopeError("no active projects")

    async def forbidden_images(top_k=6, kind="matbang", project_key=None):
        raise AssertionError("media fetch must be skipped after a scope failure")

    monkeypatch.setattr(hello_mod, "resolve_project_key", raise_scope)
    monkeypatch.setattr(hello_mod, "search_project_images", forbidden_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", lambda *a, **k: [_video(0)])

    resp = run(hello_mod.llms_hello(request(), hello_mod.HelloRequest(project_key="ghost")))

    assert resp.greeting == "Chào ạ"
    assert resp.images == []
    assert len(resp.videos) == 1


def test_llms_hello_project_scope_passes_key_to_media(monkeypatch):
    # A resolved project key must scope the media resolution: image search and
    # the video registry both receive "soleil", never the default project.
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào Anh/Chị dự án Soleil"))

    async def fake_resolve(project_key):
        return "soleil"

    async def fake_registry(project_key):
        return None  # offline: bound_request_project(None) serves static defaults

    seen: dict = {}

    async def scoped_images(top_k=6, kind="matbang", project_key=None):
        seen["images"] = project_key
        return [_image(i) for i in range(2)]

    def scoped_videos(project_key="camellia"):
        seen["videos"] = project_key
        return [_video(0)]

    monkeypatch.setattr(hello_mod, "resolve_project_key", fake_resolve)
    monkeypatch.setattr(hello_mod, "load_project_registry_record", fake_registry)
    monkeypatch.setattr(hello_mod, "search_project_images", scoped_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", scoped_videos)

    resp = run(hello_mod.llms_hello(request(), hello_mod.HelloRequest(project_key="soleil")))

    assert resp.greeting == "Chào Anh/Chị dự án Soleil"
    assert seen == {"images": "soleil", "videos": "soleil"}
    assert len(resp.images) == 2
    assert len(resp.videos) == 1


def test_llms_hello_images_empty_when_search_degrades(monkeypatch):
    # search_project_images is best-effort and returns [] on DB/embed failure;
    # the recent-rows fallback then supplies the gallery for a project-scoped
    # greeting, which still succeeds.
    monkeypatch.setattr(hello_mod, "get_llm", lambda: _FakeLLM("Chào ạ"))

    async def fake_resolve(project_key):
        return "soleil"

    async def fake_registry(project_key):
        return None  # offline: bound_request_project(None) serves static defaults

    async def empty_images(top_k=6, kind="matbang", project_key=None):
        return []

    async def fallback_images(project_key="camellia", limit=8):
        return [_image(i) for i in range(3)]

    def fake_videos(project_key="camellia"):
        return [_video(0)]

    monkeypatch.setattr(hello_mod, "resolve_project_key", fake_resolve)
    monkeypatch.setattr(hello_mod, "load_project_registry_record", fake_registry)
    monkeypatch.setattr(hello_mod, "search_project_images", empty_images)
    monkeypatch.setattr(hello_mod, "fetch_recent_project_images", fallback_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", fake_videos)

    resp = run(hello_mod.llms_hello(request(), hello_mod.HelloRequest(project_key="soleil")))

    assert resp.greeting == "Chào ạ"
    assert len(resp.images) == 3
    assert len(resp.videos) == 1
