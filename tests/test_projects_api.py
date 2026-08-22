"""Story 10.3: GET /api/projects catalogue + PROJECT_SCOPE 422 projects array.

The FE project picker reads the real active-project list from this endpoint (and
from the 422 body when a scope choice is required) to show detailed Vietnamese
addresses and lead with the HOT project. All DB reads are mocked here — the
loader's best-effort degradation (projects: [] on a dead DB) is exercised too.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.application.services import project_scope
from api.interfaces.api.main import create_app

# Contract-shaped catalogue mirroring db/seed/project_config.sql after the
# location/is_hot migration + seed re-run. Kept in loader-output order (HOT
# first) so it represents what the real fetch_projects() returns.
_CATALOGUE = [
    {
        "project_key": "camellia",
        "name": "The Camellia Son Tra - Da Nang",
        "location": "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, Đà Nẵng",
        "lat": 16.1052,
        "lng": 108.2558,
        "is_hot": True,
    },
    {
        "project_key": "soleil",
        # Verbatim ten_thuong_mai from the seed contract (kept whole).
        "name": "The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite Collection)",  # noqa: E501
        "location": "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
        "lat": 16.0710756,
        "lng": 108.2436243,
        "is_hot": False,
    },
]

_CONTRACT_KEYS = {"project_key", "name", "location", "lat", "lng", "is_hot"}


def _make_client(monkeypatch, projects: list[dict]) -> TestClient:
    """App with the catalogue loader stubbed so the DB never runs."""
    monkeypatch.setattr(
        "api.application.services.project_config.fetch_projects",
        lambda: projects,
    )
    return TestClient(create_app())


# --- GET /api/projects -------------------------------------------------------

def test_projects_endpoint_returns_contract_shape(monkeypatch) -> None:
    client = _make_client(monkeypatch, list(_CATALOGUE))
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"projects"}
    projects = body["projects"]
    assert len(projects) == 2
    for project in projects:
        assert set(project) == _CONTRACT_KEYS
        assert isinstance(project["lat"], float)
        assert isinstance(project["lng"], float)
        assert isinstance(project["is_hot"], bool)
    by_key = {p["project_key"]: p for p in projects}
    assert by_key["camellia"]["is_hot"] is True
    assert by_key["camellia"]["name"] == "The Camellia Son Tra - Da Nang"
    assert "Thọ Quang" in by_key["camellia"]["location"]
    assert by_key["soleil"]["is_hot"] is False
    assert by_key["soleil"]["lat"] == 16.0710756


def test_projects_endpoint_sorted_hot_first(monkeypatch) -> None:
    # The endpoint must surface the loader output as-is; the loader guarantees
    # the HOT-first order. Feed a catalogue whose only ordering signal is the
    # loader's — the first item is the HOT project.
    client = _make_client(monkeypatch, list(_CATALOGUE))
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    projects = resp.json()["projects"]
    assert [p["project_key"] for p in projects] == ["camellia", "soleil"]
    assert projects[0]["is_hot"] is True


def test_fetch_projects_sorts_hot_first_then_name(monkeypatch) -> None:
    """The loader re-sorts unsorted DB rows: HOT first, then name."""
    import psycopg2

    from api.application.services import project_config as pc

    # Unsorted DB rows: soleil (non-hot) listed before camellia (hot).
    rows = [
        (
            "soleil",
            "The Soleil Đà Nẵng (Bộ sưu tập căn hộ khách sạn hạng thương gia - C Suite Collection)",
            "Giao lộ Phạm Văn Đồng - Võ Nguyên Giáp, quận Sơn Trà, Đà Nẵng",
            16.0710756,
            108.2436243,
            False,
        ),
        (
            "camellia",
            "The Camellia Son Tra - Da Nang",
            "Giao lộ Lê Văn Lương - Lê Đức Thọ, phường Thọ Quang, quận Sơn Trà, Đà Nẵng",
            16.1052,
            108.2558,
            True,
        ),
    ]

    class _FakeCursor:
        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def execute(self, sql, params=None) -> None:
            # The read must stay scoped to active projects (RLS + explicit WHERE).
            assert "status = 'active'" in sql

        def fetchall(self) -> list[tuple]:
            return rows

    class _FakeConn:
        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *exc) -> bool:
            return False

    def _fake_connect(dsn, connect_timeout: int) -> _FakeConn:
        assert connect_timeout == 2  # short timeout — best-effort contract
        return _FakeConn()

    monkeypatch.setattr(psycopg2, "connect", _fake_connect)
    projects = pc.fetch_projects()
    assert [p["project_key"] for p in projects] == ["camellia", "soleil"]
    assert projects[0]["is_hot"] is True


def test_projects_endpoint_db_dead_returns_empty(monkeypatch) -> None:
    """A dead DB degrades to 200 projects: [] — never a 500."""
    import psycopg2

    def _boom(dsn, connect_timeout: int) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(psycopg2, "connect", _boom)
    client = TestClient(create_app())
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == {"projects": []}


# --- /query 422 PROJECT_SCOPE body -------------------------------------------

def test_query_422_project_scope_includes_projects(monkeypatch) -> None:
    from api.application.services.project_scope import ProjectScopeError

    async def _require_choice(requested, *, active_projects=None) -> str:
        raise ProjectScopeError(project_scope.PROJECT_CHOICE_REQUIRED)

    monkeypatch.setattr(
        "api.application.services.project_scope.resolve_project_key", _require_choice
    )
    monkeypatch.setattr(
        "api.application.services.project_config.fetch_projects",
        lambda: list(_CATALOGUE),
    )
    client = TestClient(create_app())
    resp = client.post("/query", json={"query": "giá 2PN bao nhiêu?"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "PROJECT_SCOPE"
    projects = body["projects"]
    assert len(projects) == 2
    assert set(projects[0]) == _CONTRACT_KEYS
    assert projects[0]["project_key"] == "camellia"
    assert projects[0]["is_hot"] is True
