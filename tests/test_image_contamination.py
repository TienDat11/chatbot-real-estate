"""Contamination suite — cross-project image/media isolation (Camellia vs Soleil).

Pins the guarantees the image-scoping hardening depends on, layer by layer, with
no live DB or embedding call:

1. SQL scope: the project predicate rides in the scoped SQL (Camellia query binds
   project_key='camellia'; a fake conn can only ever return what it is handed, so
   the SQL contract is what prevents Soleil rows from entering the pool at all).
2. URL policy: ``valid_media_url`` enforces the per-project CDN map (origin AND
   object-key namespace), so even a row that sneaked in with a foreign URL is
   dropped at shaping time.
3. Embedding identity: scoped vector SQL filters rows to the active (model, dims)
   pair, so vectors from another embedding space are excluded, not scored.
4. Fail-closed: project_key=None/unknown -> [] before any embed/DB work.
5. Greeting strictness: search_project_images([] for None), list_project_videos
   (no Camellia static-borrow for Soleil), and the hello handlers pass the
   resolved project key through.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import quote

import api.application.services.image_search as img
import api.application.services.media_config as media_config
from api.infrastructure.config.config import settings

SHARED_HOST = "https://shared-r2.example.test"
CAMELLIA_URL = f"{SHARED_HOST}/images/camellia/matbang/cover.png"
SOLEIL_URL = f"{SHARED_HOST}/images/soleil/matbang/cover.png"
FOREIGN_URL = "https://evil.example.test/images/camellia/x.png"

# Explicit per-project map on ONE shared host: isolation then rests entirely on
# path prefixes, exactly the production layout (both projects share the R2 host).
_PROJECT_MAP = (
    '{"camellia":{"origins":["%s"],"path_prefixes":["images/camellia/","images/matbang/","images/banggia/","media/video/"]},'
    '"soleil":{"origins":["%s"],"path_prefixes":["images/soleil/"]}}' % (SHARED_HOST, SHARED_HOST)
)


def run(coro):
    return asyncio.run(coro)


def _row(image_id="img-1", url=CAMELLIA_URL, project_key="camellia", **overrides):
    row = {
        "image_id": image_id,
        "kind": "matbang",
        "title": f"t-{image_id}",
        "caption": "Mặt bằng",
        "alt_text": "mb",
        "url_cdn": url,
        "width": 100,
        "height": 100,
        "linked_subject_key": None,
        "metadata": {},
        "project_key": project_key,
        "score": 0.9,
    }
    row.update(overrides)
    return row


class _RecordingConn:
    """Captures every fetch call (sql + args) and replays canned rows per SQL tag."""

    def __init__(self, rows_by_tag):
        self._rows_by_tag = rows_by_tag  # {"camellia": [...], "soleil": [...]}
        self.calls: list[tuple[str, tuple]] = []

    def _tag(self, sql):
        for tag in self._rows_by_tag:
            if f"project_key = ${_pos(sql, 'project_key')}" in sql or (
                "project_key" in sql and f"'{tag}'" in (sql, "")
            ):
                return tag
        return None

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        # Serve rows only for the project whose key was BOUND into the SQL args.
        # The suite asserts the bound value, so rows for the other project are
        # never even available to the caller — mirroring a correctly-scoped DB.
        for tag, rows in self._rows_by_tag.items():
            if tag in args:
                return rows
        return []

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None


def _pos(sql, _name):
    return 0  # unused; tag binding is arg-value based (see _RecordingConn.fetch)


@asynccontextmanager
async def _fake_rls(conn):
    yield conn


# ---------------------------------------------------------------- 1 + 2: scope


def test_camellia_query_urls_are_100pct_camellia_policy(monkeypatch):
    """A Camellia query returns only URLs the Camellia policy accepts."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(_RecordingConn({"camellia": [_row(i, project_key="camellia")] for i in range(4)})))

    async def fake_embed(_text):
        return [0.1] * 1024

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    out = run(img.search_images("tiện ích dự án", project_key="camellia"))
    assert out, "semantic path returned no images"
    assert all(u["url_cdn"] and u["url_cdn"].startswith(f"{SHARED_HOST}/images/camellia/") for u in out)
    # Every URL passes the project's own policy seam (origin AND namespace).
    assert all(media_config.valid_media_url(u["url_cdn"], "camellia", "gallery") for u in out)


def test_soleil_query_urls_are_100pct_soleil_policy(monkeypatch):
    """A Soleil query returns only URLs the Soleil policy accepts."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(_RecordingConn({"soleil": [_row(i, url=SOLEIL_URL, project_key="soleil")] for i in range(4)})))

    async def fake_embed(_text):
        return [0.1] * 1024

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    out = run(img.search_images("tiện ích dự án", project_key="soleil"))
    assert out
    assert all(u["url_cdn"].startswith(f"{SHARED_HOST}/images/soleil/") for u in out)
    assert all(media_config.valid_media_url(u["url_cdn"], "soleil", "gallery") for u in out)


def test_price_board_is_scoped_and_namespace_checked(monkeypatch):
    """Full-board fetch binds the project key and drops foreign-namespace URLs."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    conn = _RecordingConn({"camellia": [_row("board-1", project_key="camellia")]})
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(conn))
    out = run(img.fetch_price_board_images("camellia"))
    assert len(out) == 1
    # The bound arg must be the requested project, not a default/legacy key.
    assert conn.calls[0][2][0] == "camellia"
    assert out[0]["url_cdn"].startswith(f"{SHARED_HOST}/images/camellia/")


# --------------------------------------------------- 3: no foreign rows ever


def test_camellia_query_never_receives_soleil_rows(monkeypatch):
    """Even when Soleil has more/older/higher-scored rows, a Camellia query only
    ever sees Camellia rows: the fake conn serves Soleil rows SOLELY when the
    SQL call binds project_key='soleil' — the assertion proves the Camellia
    request never binds the foreign key (SQL-level scope, not post-filtering)."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    soleil_rows = [_row(f"s-{i}", url=SOLEIL_URL, project_key="soleil", score=0.99) for i in range(6)]
    camellia_rows = [_row(f"c-{i}", project_key="camellia", score=0.5) for i in range(2)]
    conn = _RecordingConn({"camellia": camellia_rows, "soleil": soleil_rows})
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(conn))

    async def fake_embed(_text):
        return [0.1] * 1024

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    out = run(img.search_images("tiện ích", project_key="camellia"))

    # Every fetch call bound 'camellia' (the Soleil bank was never touched).
    for _, sql, args in conn.calls:
        assert "soleil" not in args, "a Camellia request bound the Soleil project key"
    assert all(u["image_id"].startswith("c-") for u in out)
    assert all(u["url_cdn"].startswith(f"{SHARED_HOST}/images/camellia/") for u in out)


def test_foreign_url_rows_are_dropped_at_shaping_time(monkeypatch):
    """Defense-in-depth: rows that DID arrive with another project's URL (a bad
    seed or a shared-host mistake) are dropped by the URL policy, not shown."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    poisoned = [
        _row("ok-1", project_key="camellia"),                      # Camellia namespace: kept
        _row("bad-soleil", url=SOLEIL_URL, project_key="camellia"),  # Soleil namespace: dropped
        _row("bad-host", url=FOREIGN_URL, project_key="camellia"),   # unmapped host: dropped
        _row("bad-path", url=f"{SHARED_HOST}/images/other/x.png", project_key="camellia"),  # dropped
    ]
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(_RecordingConn({"camellia": poisoned})))

    async def fake_embed(_text):
        return [0.1] * 1024

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    out = run(img.search_images("tiện ích", project_key="camellia"))
    assert [u["image_id"] for u in out] == ["ok-1"]


def test_legacy_banggia_namespace_is_camellia_only(monkeypatch):
    """The legacy images/banggia/ prefix defaults to Camellia ONLY; Soleil must
    not inherit it via the shared host (Soleil price pages live in the manifest
    under images/soleil/)."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    assert media_config.valid_media_url(f"{SHARED_HOST}/images/banggia/gia-1.png", "camellia", "gallery")
    assert media_config.valid_media_url(f"{SHARED_HOST}/images/banggia/gia-1.png", "soleil", "gallery") is None
    assert media_config.valid_media_url(f"{SHARED_HOST}/images/soleil/gia-1.png", "soleil", "gallery")


# ------------------------------------------------- 3b: embedding-identity guard


def test_scoped_sql_excludes_rows_from_other_embedding_space(monkeypatch):
    """The scoped vector SQL filters on the active (model, dims) identity, so a
    row embedded by another model is excluded rather than scored."""
    conn = _RecordingConn({"camellia": [_row("c-1")]})
    captured: dict = {}

    class _GuardedConn(_RecordingConn):
        async def fetch(self, sql, *args):
            captured["sql"] = sql
            captured["args"] = args
            return await super().fetch(sql, *args)

    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(_GuardedConn({"camellia": [_row("c-1")]})))

    async def fake_embed(_text):
        return [0.1] * 1024

    monkeypatch.setattr(img, "_embed_query", fake_embed)
    run(img.search_images("tiện ích", project_key="camellia"))

    sql = captured["sql"]
    assert "e.model = $4" in sql and "e.dims = $5" in sql
    assert "i.project_key = $3" in sql
    model, dims = captured["args"][3], captured["args"][4]
    expected_model, expected_dims = img._embedding_identity()
    assert model == expected_model and dims == expected_dims == 1024


# --------------------------------------------------------------- 4: fail-closed


def test_missing_project_key_is_empty_without_embed_or_db(monkeypatch):
    """project_key=None -> [] immediately; no embedding call, no DB round-trip."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)

    async def forbidden_embed(_text):
        raise AssertionError("embedding must not be called without a project key")

    class _ForbiddenConn:
        async def fetch(self, *a, **k):
            raise AssertionError("DB must not be queried without a project key")

    monkeypatch.setattr(img, "_embed_query", forbidden_embed)
    monkeypatch.setattr(img, "with_rls_identity", lambda: _fake_rls(_ForbiddenConn()))

    assert run(img.search_images("bảng giá", project_key=None)) == []
    assert run(img.search_project_images(project_key=None)) == []


def test_unknown_project_yields_no_media(monkeypatch):
    """An unmapped project key authorizes no origins: every URL is rejected."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    assert media_config.allowed_media_origins("ghost") == set()
    assert media_config.valid_media_url(f"{SHARED_HOST}/images/camellia/x.png", "ghost", "gallery") is None
    assert media_config.valid_media_url(f"{SHARED_HOST}/images/soleil/x.png", "ghost", "gallery") is None


def test_null_bytes_and_traversal_rejected_before_namespace_check(monkeypatch):
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)
    # %2e%2e decodes to '..' only inside valid_media_url's unquote — must reject.
    assert media_config.valid_media_url(
        f"{SHARED_HOST}/images/camellia/{quote('../../soleil/x.png')}", "camellia", "gallery"
    ) is None


# ------------------------------------------------------- 5: greeting strictness


def test_greeting_videos_never_borrow_camellia_bundle(monkeypatch):
    """Soleil (media=[]) and a ghost project get [], never Camellia's clips."""
    assert media_config.list_project_videos("soleil") == []
    assert media_config.list_project_videos("ghost") == []
    # The unbound sync read also fails closed on a registry miss.
    monkeypatch.setattr(media_config, "_media_from_registry", lambda _key: None)
    assert media_config.list_project_videos("soleil") == []


def test_greeting_media_uses_resolved_project_key(monkeypatch):
    """hello handlers pass the RESOLVED key to both media sources (no default)."""
    from api.interfaces.api import hello as hello_mod
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/llms-hello", "headers": []})

    class _LLM:
        async def complete(self, messages, **kwargs):
            return "Chào ạ"

    seen: dict = {}

    async def fake_resolve(project_key):
        return "soleil"

    async def fake_registry(project_key):
        return None

    async def scoped_images(top_k=6, kind="matbang", project_key=None):
        seen["images"] = project_key
        return []

    async def scoped_recent(project_key, limit=8):
        seen["recent"] = project_key
        return []

    def scoped_videos(project_key="camellia"):
        seen["videos"] = project_key
        return []

    monkeypatch.setattr(hello_mod, "get_llm", lambda: _LLM())
    monkeypatch.setattr(hello_mod, "resolve_project_key", fake_resolve)
    monkeypatch.setattr(hello_mod, "load_project_registry_record", fake_registry)
    monkeypatch.setattr(hello_mod, "search_project_images", scoped_images)
    monkeypatch.setattr(hello_mod, "fetch_recent_project_images", scoped_recent)
    monkeypatch.setattr(hello_mod, "list_project_videos", scoped_videos)

    resp = asyncio.run(hello_mod.llms_hello(req, hello_mod.HelloRequest(project_key="soleil")))
    # Both media legs (and the recent-rows fallback) receive the resolved key.
    assert seen == {"images": "soleil", "recent": "soleil", "videos": "soleil"}
    assert resp.images == [] and resp.videos == []


def test_greeting_scope_failure_returns_no_foreign_media(monkeypatch):
    """A scope failure skips media fetches entirely (images stay empty)."""
    from api.interfaces.api import hello as hello_mod
    from api.application.services.project_scope import ProjectScopeError
    from starlette.requests import Request

    req = Request({"type": "http", "method": "GET", "path": "/llms-hello", "headers": []})

    class _LLM:
        async def complete(self, messages, **kwargs):
            return "Chào ạ"

    async def raise_scope(project_key):
        raise ProjectScopeError("no active projects")

    async def forbidden_images(top_k=6, kind="matbang", project_key=None):
        raise AssertionError("media fetch must be skipped after a scope failure")

    monkeypatch.setattr(hello_mod, "get_llm", lambda: _LLM())
    monkeypatch.setattr(hello_mod, "resolve_project_key", raise_scope)
    monkeypatch.setattr(hello_mod, "search_project_images", forbidden_images)
    monkeypatch.setattr(hello_mod, "list_project_videos", lambda *a, **k: [])

    resp = asyncio.run(hello_mod.llms_hello(req, hello_mod.HelloRequest(project_key="ghost")))
    assert resp.images == [] and resp.videos == []


def test_recent_rows_fallback_is_project_scoped_and_policy_checked(monkeypatch):
    """fetch_recent_project_images filters rows through the project's policy so a
    foreign-namespace row in the recent gallery cannot leak."""
    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)

    class _FakeRegistry:
        async def fetch_recent_published_images(self, project_key, limit):
            assert project_key == "soleil"
            return [
                {"image_id": "good", "url_cdn": SOLEIL_URL},
                {"image_id": "bad", "url_cdn": CAMELLIA_URL},
            ]

    import api.infrastructure.dependencies as deps

    monkeypatch.setattr(deps, "get_project_registry", lambda: _FakeRegistry())
    out = run(media_config.fetch_recent_project_images("soleil"))
    assert [r["image_id"] for r in out] == ["good"]
    assert out[0]["url_cdn"].startswith(f"{SHARED_HOST}/images/soleil/")


def test_registry_recent_gallery_drops_same_origin_wrong_namespace(monkeypatch):
    """PostgresProjectRegistry.fetch_recent_published_images validates with the
    "gallery" media kind (matching fetch_recent_project_images): a same-origin
    URL outside the project's object-key namespace is DROPPED, not passed
    through with only an origin check."""
    import asyncio as _asyncio

    from api.infrastructure.adapters.postgres_project_registry import (
        PostgresProjectRegistry,
    )

    monkeypatch.setattr(settings, "image_cdn_project_map", _PROJECT_MAP)

    rows = [
        ("rg-good", "matbang", "good", None, "ok", SOLEIL_URL, 100, 100),
        # Same shared host, but the camellia namespace: origin passes, path fails.
        ("rg-camellia", "matbang", "wrong-ns", None, "bad", CAMELLIA_URL, 100, 100),
        # Same host, no project namespace at all.
        ("rg-unprefixed", "matbang", "unprefixed", None, "bad", f"{SHARED_HOST}/x.png", 100, 100),
    ]

    class _FakePool:
        def __init__(self, rows):
            self._rows = rows

        def acquire(self):
            outer = self

            class _ConnCtx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def fetch(self, _sql, *args):
                    # Bind the rows to the requested project like the real DB.
                    return outer._rows if args and args[0] == "soleil" else []

            return _ConnCtx()

    adapter = PostgresProjectRegistry()
    # Bypass the real pool creation; only the row-shaping policy is under test.
    async def fake_pool():
        return _FakePool(rows)

    adapter._get_pool = fake_pool  # type: ignore[method-assign]

    out = _asyncio.run(adapter.fetch_recent_published_images("soleil", 8))
    assert [r["image_id"] for r in out] == ["rg-good"]
    assert out[0]["url_cdn"] == SOLEIL_URL
