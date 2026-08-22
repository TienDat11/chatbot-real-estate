"""Settings extension — Firebase realtime binding for the BE (append-only edit)."""

from __future__ import annotations

from api.infrastructure.config.config import get_settings


def test_settings_expose_firebase_realtime_configuration() -> None:
    settings = get_settings()
    assert settings.firebase_binding == "off"
    assert settings.firebase_project_id == "sale-chat-bot-11e49"
    assert settings.firebase_firestore_rest_base_url == "https://firestore.googleapis.com/v1"
    assert settings.firebase_jwks_url.startswith("https://www.googleapis.com/service_accounts/v1/jwk/")
    assert settings.firebase_auth_issuer.endswith(settings.firebase_project_id)
