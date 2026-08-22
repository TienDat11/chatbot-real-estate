"""Wave-1 integration tests: real Postgres + real TestClient, no mocks.

Covers the seams unit tests paper over with fakes:
  - GET /api/projects serves the registry (hot-first, with locations);
  - POST /api/query PROJECT_SCOPE 422 carries the picker payload;
  - POST /api/lead persists project_key + device_id on the real row;
  - /llms-hello media is scoped to the requested project (Soleil gallery rows
    registered by ingest.register_project_images never leak Camellia media and
    vice versa).

The whole module skips when the dev database is unreachable so `pytest tests/`
stays green on machines without Postgres.
"""

from __future__ import annotations

import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient

from api.infrastructure.config.config import settings
from api.interfaces.api.main import create_app


def _db_reachable() -> bool:
    try:
        conn = psycopg2.connect(settings.pg_dsn_sync, connect_timeout=3)
    except psycopg2.OperationalError:
        return False
    conn.close()
    return True


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="integration tests need the dev Postgres"
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app())


class TestProjectsEndpoint:
    def test_serves_registry_hot_first_with_locations(self, client: TestClient):
        response = client.get("/api/projects")
        assert response.status_code == 200
        projects = response.json()["projects"]
        assert [p["project_key"] for p in projects][:2] == ["camellia", "soleil"]
        assert projects[0]["is_hot"] is True
        assert not projects[1]["is_hot"]
        for project in projects:
            assert project["location"], "every registry project carries an address"
            assert isinstance(project["lat"], float)
            assert isinstance(project["lng"], float)


class TestProjectScope422:
    def test_query_without_choice_carries_projects_payload(self, client: TestClient):
        response = client.post(
            "/query",
            json={
                "query": "bảng giá",
                "session_id": str(uuid.uuid4()),
                "project_key": "",
            },
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "PROJECT_SCOPE"
        keys = [p["project_key"] for p in body["projects"]]
        assert keys[0] == "camellia"  # hot project leads the picker


class TestLeadPersistence:
    def test_lead_row_carries_project_and_device(self, client: TestClient):
        phone = f"09{uuid.uuid4().int % 10**8:08d}"
        device_id = str(uuid.uuid4())
        response = client.post(
            "/api/lead",
            json={
                "phone": phone,
                "name": "IT Wave1",
                "project_key": "soleil",
                "device_id": device_id,
                "session_id": str(uuid.uuid4()),
                "need": "2PN view bien",
                "consent": True,
            },
        )
        assert response.status_code in (200, 201), response.text

        with psycopg2.connect(settings.pg_dsn_sync) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT project_key, device_id FROM leads "
                    "WHERE phone = %s ORDER BY created_at DESC LIMIT 1",
                    (phone,),
                )
                row = cur.fetchone()
                assert row is not None, "lead must land in the real table"
                assert row[0] == "soleil"
                assert row[1] == device_id
                cur.execute("DELETE FROM leads WHERE phone = %s", (phone,))


class TestHelloMediaScoping:
    def test_soleil_hello_media_is_project_scoped(self, client: TestClient):
        response = client.post(
            "/llms-hello",
            json={"project_key": "soleil", "session_id": str(uuid.uuid4())},
        )
        assert response.status_code == 200
        payload = response.json()
        assert "Soleil" in payload["greeting"], payload["greeting"][:120]

        images = payload["images"]
        assert images, "Soleil has 66 registered floor plans"
        image_ids = [img["image_id"] for img in images]
        assert all(img_id.startswith("soleil-") for img_id in image_ids), (
            "no Camellia image may leak into a Soleil greeting"
        )
        # Videos stay honest: no Soleil media file exists yet.
        assert payload["videos"] == []

    def test_camellia_hello_never_serves_soleil_rows(self, client: TestClient):
        response = client.post(
            "/llms-hello",
            json={"project_key": "camellia", "session_id": str(uuid.uuid4())},
        )
        assert response.status_code == 200
        images = response.json()["images"]
        assert images
        assert all(
            not img["image_id"].startswith("soleil-") for img in images
        )
