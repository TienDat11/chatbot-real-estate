"""Unit tests for LightRAG per-workspace routing across ingest, query, and init.

Requirements:
- project_workspace('soleil') returns the configured soleil workspace;
  'camellia' and None return None.
- get_lightrag(workspace) builds and caches one instance per workspace:
  same workspace twice -> same object; different workspaces -> distinct objects;
  LightRAG constructor receives the workspace kwarg when provided.
- rag_leg._get_rag(project_key): soleil -> workspace passed through;
  camellia/None -> default instance.
- ingest/load.py: the LightRAG insert block calls get_lightrag with
  project_workspace(...) for a soleil document.
Mock-based: NO real DB, NO network, NO real LightRAG.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from api.infrastructure.config.config import Settings


@pytest.fixture(autouse=True)
def _reset_lightrag_instances():
    """Ensure _lightrag_instances cache in ingest.lightrag_init is clean before/after tests."""
    from ingest import lightrag_init

    with lightrag_init._lock:
        lightrag_init._lightrag_instances.clear()
    yield
    with lightrag_init._lock:
        lightrag_init._lightrag_instances.clear()


# ==============================================================================
# 1. project_workspace
# ==============================================================================


def test_project_workspace_routing(monkeypatch):
    """project_workspace('soleil') returns the configured soleil workspace;

    'camellia' and None return None.
    """
    from ingest.lightrag_init import project_workspace
    from api.infrastructure.config.config import get_settings

    # Verify default config value
    settings = get_settings()
    assert project_workspace("soleil") == settings.lightrag_workspace_soleil
    assert project_workspace("camellia") is None
    assert project_workspace(None) is None
    assert project_workspace("random_project") is None

    # Verify dynamic/monkeypatched config value
    monkeypatch.setattr(settings, "lightrag_workspace_soleil", "custom_soleil_ws", raising=False)
    assert project_workspace("soleil") == "custom_soleil_ws"


@pytest.mark.parametrize("bad_ws", ["default", "Default", "DEFAULT", "  default  "])
def test_settings_default_namespace_value_rejected(bad_ws):
    """Settings must reject the PG storage default namespace value 'default'
    (postgres_impl.py:2700-2702 maps empty workspace to 'default').
    """
    with pytest.raises(ValidationError, match="must not be 'default'"):
        Settings(app_env="dev", lightrag_workspace_soleil=bad_ws)


@pytest.mark.parametrize("bad_ws", ["default", "Default", "DEFAULT", ""])
def test_project_workspace_rejects_default_namespace_value(monkeypatch, bad_ws):
    """project_workspace('soleil') must raise ValueError when the resolved
    workspace equals the shared default namespace (empty or 'default').
    """
    from ingest.lightrag_init import project_workspace
    from api.infrastructure.config.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "lightrag_workspace_soleil", bad_ws, raising=False)
    with pytest.raises(ValueError, match="shared default namespace"):
        project_workspace("soleil")


# ==============================================================================
# 2. get_lightrag caching and workspace constructor kwarg
# ==============================================================================


def test_get_lightrag_caching_and_workspace_kwarg(monkeypatch):
    """get_lightrag(workspace) builds and caches one instance per workspace.

    - Same workspace twice -> exact same object
    - Different workspaces -> distinct objects
    - LightRAG constructor receives the workspace kwarg when provided
    """
    from ingest import lightrag_init

    created_instances: list[dict[str, Any]] = []

    class DummyLightRAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_instances.append(kwargs)

    # Mock dependencies of get_lightrag
    monkeypatch.setattr(lightrag_init, "_make_embedding_func", lambda: MagicMock())
    monkeypatch.setattr(lightrag_init, "_make_llm_func", lambda: MagicMock())

    with patch.dict("sys.modules", {"lightrag": MagicMock(LightRAG=DummyLightRAG, QueryParam=MagicMock())}):
        # 1) Call with None (default workspace)
        rag_default_1 = lightrag_init.get_lightrag(None)
        assert len(created_instances) == 1
        assert "workspace" not in created_instances[0]

        # Call with None again -> cached, no new instance created
        rag_default_2 = lightrag_init.get_lightrag(None)
        assert rag_default_1 is rag_default_2
        assert len(created_instances) == 1

        # 2) Call with explicit workspace 'ragre_mvp'
        rag_soleil_1 = lightrag_init.get_lightrag("ragre_mvp")
        assert len(created_instances) == 2
        assert created_instances[1].get("workspace") == "ragre_mvp"
        assert rag_soleil_1 is not rag_default_1

        # Call with 'ragre_mvp' again -> cached
        rag_soleil_2 = lightrag_init.get_lightrag("ragre_mvp")
        assert rag_soleil_1 is rag_soleil_2
        assert len(created_instances) == 2

        # 3) Call with another workspace 'workspace_b'
        rag_other = lightrag_init.get_lightrag("workspace_b")
        assert len(created_instances) == 3
        assert created_instances[2].get("workspace") == "workspace_b"
        assert rag_other is not rag_soleil_1
        assert rag_other is not rag_default_1


# ==============================================================================
# 3. rag_leg._get_rag(project_key)
# ==============================================================================


@pytest.mark.asyncio
async def test_rag_leg_get_rag_routes_workspace():
    """rag_leg._get_rag(project_key):

    - soleil -> workspace 'ragre_mvp' passed through to get_lightrag
    - camellia/None -> default instance (workspace None) passed through
    """
    from api.application.services import rag_leg

    from api.infrastructure.config.config import get_settings

    settings = get_settings()
    soleil_ws = settings.lightrag_workspace_soleil

    # Reset storages set so initialize_storages can be tested cleanly
    rag_leg._api_storages_ready_workspaces.clear()

    mock_rag_default = MagicMock()
    mock_rag_default.initialize_storages = AsyncMock()

    mock_rag_soleil = MagicMock()
    mock_rag_soleil.initialize_storages = AsyncMock()

    calls: list[str | None] = []

    def fake_get_lightrag(workspace: str | None = None):
        calls.append(workspace)
        if workspace == soleil_ws:
            return mock_rag_soleil
        return mock_rag_default

    with patch("ingest.lightrag_init.get_lightrag", side_effect=fake_get_lightrag):
        # 1) Query with project_key='soleil'
        res_soleil = await rag_leg._get_rag("soleil")
        assert res_soleil is mock_rag_soleil
        assert calls[-1] == soleil_ws
        mock_rag_soleil.initialize_storages.assert_awaited_once()

        # 2) Query with project_key='camellia'
        res_camellia = await rag_leg._get_rag("camellia")
        assert res_camellia is mock_rag_default
        assert calls[-1] is None
        mock_rag_default.initialize_storages.assert_awaited_once()

        # 3) Query with project_key=None
        res_none = await rag_leg._get_rag(None)
        assert res_none is mock_rag_default
        assert calls[-1] is None


# ==============================================================================
# 4. ingest/load.py: post-commit LightRAG insert block routes workspace
# ==============================================================================


@pytest.mark.asyncio
async def test_load_document_routes_lightrag_workspace_for_soleil():
    """ingest/load.py calls get_lightrag with project_workspace(resolved_project_key).

    For a Soleil document ('price-soleil-2026q3-policy'), get_lightrag is called
    with the configured soleil workspace.
    """
    from api.infrastructure.config.config import get_settings
    from ingest.load import load_document
    from ingest.parser import ParsedDoc, ParsedSection

    settings = get_settings()
    soleil_ws = settings.lightrag_workspace_soleil

    parsed = ParsedDoc(
        doc_id="price-soleil-2026q3-policy",
        title="Chính sách bán hàng Soleil",
        kind="price",
        source_file="soleil_policy.pdf",
        sections=[ParsedSection(text="Nội dung chính sách bán hàng Soleil.")],
        content_hash="dummyhash123",
        effective_from=datetime.date(2026, 7, 1),
        effective_to=None,
        metadata={},
        project_key="soleil",
    )

    # Mock asyncpg connection and transaction
    mock_conn = MagicMock()
    mock_conn.close = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_conn.fetchrow = AsyncMock(return_value={"version": 1})

    class DummyTransaction:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    mock_conn.transaction = MagicMock(return_value=DummyTransaction())

    mock_rag = MagicMock()
    recorded_workspaces: list[str | None] = []

    def fake_get_lightrag(workspace: str | None = None):
        recorded_workspaces.append(workspace)
        return mock_rag

    with (
        patch("asyncpg.connect", AsyncMock(return_value=mock_conn)),
        patch("ingest.lightrag_init.get_lightrag", side_effect=fake_get_lightrag),
        patch("ingest.lightrag_init.ainsert_document", AsyncMock()) as mock_ainsert,
    ):
        result = await load_document(parsed, facts=[])

        assert result.doc_id == "price-soleil-2026q3-policy"
        assert len(recorded_workspaces) == 1
        # Soleil doc must be routed to configured soleil workspace
        assert recorded_workspaces[0] == soleil_ws
        mock_ainsert.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_document_routes_lightrag_workspace_default_for_untagged():
    """For an untagged document (project_key is None and doc_id has no known project token),

    get_lightrag is called with None (default workspace).
    """
    from ingest.load import load_document
    from ingest.parser import ParsedDoc, ParsedSection

    parsed = ParsedDoc(
        doc_id="legal-nd101-2024",
        title="Nghị định 101/2024",
        kind="legal",
        source_file="nd101.pdf",
        sections=[ParsedSection(text="Quy định chi tiết...")],
        content_hash="dummyhash456",
        effective_from=datetime.date(2024, 8, 1),
        effective_to=None,
        metadata={},
        project_key=None,
    )

    mock_conn = MagicMock()
    mock_conn.close = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=1)
    mock_conn.fetchrow = AsyncMock(return_value={"version": 1})

    class DummyTransaction:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    mock_conn.transaction = MagicMock(return_value=DummyTransaction())

    mock_rag = MagicMock()
    recorded_workspaces: list[str | None] = []

    def fake_get_lightrag(workspace: str | None = None):
        recorded_workspaces.append(workspace)
        return mock_rag

    with (
        patch("asyncpg.connect", AsyncMock(return_value=mock_conn)),
        patch("ingest.lightrag_init.get_lightrag", side_effect=fake_get_lightrag),
        patch("ingest.lightrag_init.ainsert_document", AsyncMock()) as mock_ainsert,
    ):
        result = await load_document(parsed, facts=[])

        assert result.doc_id == "legal-nd101-2024"
        assert len(recorded_workspaces) == 1
        # Untagged doc routes to None (default workspace)
        assert recorded_workspaces[0] is None
        mock_ainsert.assert_awaited_once()
