from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from api.infrastructure.config.config import Settings
from api.interfaces.api.main import create_app


class _BrokenConnection:
    async def fetchval(self, query: str) -> int:
        raise RuntimeError("db-host internal-user credential-exception")


class _ConnectionContext:
    async def __aenter__(self) -> _BrokenConnection:
        return _BrokenConnection()

    async def __aexit__(self, *args: object) -> None:
        return None


class _BrokenPool:
    def acquire(self) -> _ConnectionContext:
        return _ConnectionContext()


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_legacy_sales_key_mode_rejected_outside_dev_test(app_env: str) -> None:
    with pytest.raises(ValueError, match="SALES_LEGACY_KEY_AUTH_ENABLED"):
        Settings(
            _env_file=None,
            app_env=app_env,
            sales_legacy_key_auth_enabled=True,
            lead_mirror_hmac_secret="lead-mirror-test-fixture-" + "x" * 32,
            anon_identity_secret="a" * 48,
            llm_api_key="configured",
            postgres_password="configured",
        )


def test_legacy_sales_key_mode_disabled_by_default() -> None:
    settings = Settings(_env_file=None, app_env="development")
    assert settings.sales_legacy_key_auth_enabled is False


def test_readiness_returns_only_stable_status_and_logs_details(monkeypatch, caplog) -> None:
    import api.application.services.sql_leg as sql_leg

    async def broken_pool() -> _BrokenPool:
        return _BrokenPool()

    monkeypatch.setattr(sql_leg, "get_ro_pool", broken_pool)
    app = create_app()
    with caplog.at_level(logging.WARNING, logger="api.main"):
        response = TestClient(app).get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["pg_status"] == "unavailable"
    assert "pg_error" not in body
    rendered = response.text
    for secret_fragment in ("db-host", "internal-user", "credential-exception"):
        assert secret_fragment not in rendered
    assert "readiness postgres check failed" in caplog.text
    assert "db-host internal-user credential-exception" in caplog.text
