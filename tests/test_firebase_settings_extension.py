"""Settings extension — Firebase realtime binding contract for the BE.

Hermetic by construction: the host .env may legitimately set FIREBASE_BINDING
to "firestore", so every asserted value is pinned through the process
environment (which outranks dotenv in pydantic-settings) before a fresh
Settings instance is built; the get_settings cache is cleared on both sides so
no other test observes the pinned instance.
"""

from __future__ import annotations

import pytest

from api.infrastructure.config.config import get_settings

_PROJECT_ID = "sale-chat-bot-11e49"


@pytest.fixture
def hermetic_settings(monkeypatch: pytest.MonkeyPatch):
    """Fresh Settings with the Firebase fields pinned off the host .env."""
    monkeypatch.setenv("FIREBASE_BINDING", "off")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", _PROJECT_ID)
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        # Rebuild from the unpatched environment for subsequent tests.
        get_settings.cache_clear()


def test_settings_expose_firebase_realtime_configuration(hermetic_settings) -> None:
    assert hermetic_settings.firebase_binding == "off"
    assert hermetic_settings.firebase_project_id == _PROJECT_ID
    assert (
        hermetic_settings.firebase_firestore_rest_base_url == "https://firestore.googleapis.com/v1"
    )
    assert hermetic_settings.firebase_jwks_url.startswith(
        "https://www.googleapis.com/service_accounts/v1/jwk/"
    )
    assert hermetic_settings.firebase_auth_issuer.endswith(_PROJECT_ID)
