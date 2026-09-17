"""ZB-02 identity correlation and secret-boundary contracts; no network."""

from __future__ import annotations

import hmac
import json
import traceback
from collections import UserDict
from types import MappingProxyType
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import SecretStr, ValidationError

from api.application.services.zalo_bot_identity import global_session_id, opaque_identity

_KEY = "identity-test-key-independent-123456789"
_CHAT = "private-chat-marker-90317"
_PREFIX = "zalo-bot:identity:v1:"


@pytest.fixture
def settings_type():
    from api.infrastructure.config.config import Settings

    class IsolatedSettings(Settings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                       dotenv_settings, file_secret_settings):
            return (init_settings,)

    return IsolatedSettings


def _settings(settings_type, **overrides):
    values = dict(
        _env_file=None, app_env="test", enforce_approved_llm_provider=False,
        anon_identity_secret="test-anon-secret-independent-123456789",
        zalo_bot_enabled=False, zalo_bot_webhook_secret="webhook-test-12345",
        zalo_bot_token="token-test-12345", zalo_bot_identity_secret="",
    )
    return settings_type(**{**values, **overrides})


def _assert_safe_error(exc, *markers):
    surfaces = (
        str(exc), repr(exc), exc.json(), repr(exc.errors()),
        json.dumps(exc.errors(), default=str),
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    )
    assert not any(marker in surface for marker in markers for surface in surfaces)


def test_identity_is_stable_namespaced_hmac_not_raw_chat():
    identity = opaque_identity(_CHAT, _KEY)
    expected = hmac.digest(_KEY.encode(), (_PREFIX + _CHAT).encode(), "sha256").hex()
    assert identity == _PREFIX + expected
    assert opaque_identity(_CHAT, _KEY) == identity
    assert _CHAT not in identity
    assert identity != _PREFIX + hmac.digest(_KEY.encode(), _CHAT.encode(), "sha256").hex()


def test_chat_key_and_session_domains_do_not_share_correlation():
    identity = opaque_identity(_CHAT, _KEY)
    session = global_session_id(identity)
    assert global_session_id(identity) == session
    assert str(UUID(session)) == session
    assert UUID(session).version == 5
    assert _CHAT not in session
    assert session != str(uuid5(NAMESPACE_URL, identity))
    assert session != str(uuid5(uuid5(NAMESPACE_URL, _PREFIX), identity))
    for other in (opaque_identity(_CHAT + "-other", _KEY),
                  opaque_identity(_CHAT, _KEY + "-other")):
        assert other != identity
        assert global_session_id(other) != session


def test_transport_rotation_does_not_change_identity_or_session(settings_type):
    original = _settings(settings_type, zalo_bot_identity_secret=_KEY)
    rotated = _settings(settings_type, zalo_bot_identity_secret=_KEY,
                        zalo_bot_token="rotated-token", zalo_bot_webhook_secret="rotated-webhook")
    identity = opaque_identity(_CHAT, original.zalo_bot_identity_secret.get_secret_value())
    after = opaque_identity(_CHAT, rotated.zalo_bot_identity_secret.get_secret_value())
    assert after == identity
    assert global_session_id(after) == global_session_id(identity)
    assert isinstance(rotated.zalo_bot_token, str)


@pytest.mark.parametrize("enabled", [False, True])
def test_missing_identity_allows_ingress_but_fails_at_use(settings_type, enabled):
    settings = _settings(settings_type, zalo_bot_enabled=enabled)
    assert settings.zalo_bot_identity_secret.get_secret_value() == ""
    with pytest.raises(ValueError):
        opaque_identity(_CHAT, settings.zalo_bot_identity_secret.get_secret_value())


@pytest.mark.parametrize("secret", ["x" * 32, "x" * 256, "密" * 32])
def test_configured_key_boundaries_and_masking(settings_type, secret):
    settings = _settings(settings_type, zalo_bot_identity_secret=secret)
    assert settings.zalo_bot_identity_secret.get_secret_value() == secret
    assert opaque_identity(_CHAT, secret).startswith(_PREFIX)
    assert secret not in repr(settings)
    assert secret not in repr(settings.model_dump())
    assert secret not in settings.model_dump_json()


@pytest.mark.parametrize("secret,markers", [
    ("too-short-private-key", ("too-short-private-key",)),
    ("private-key-" * 24, ("private-key-",)),
    (["private-list-key"], ("private-list-key",)),
    ({"value": "private-dict-key"}, ("private-dict-key",)),
    (739182640517, ("739182640517",)),
    (None, ()),
], ids=["short", "long", "list", "dict", "integer", "none"])
@pytest.mark.parametrize("wrapped", [False, True], ids=["raw", "SecretStr"])
def test_invalid_identity_secret_safe_on_all_surfaces(settings_type, secret, markers, wrapped):
    with pytest.raises(ValidationError) as caught:
        _settings(settings_type, zalo_bot_identity_secret=SecretStr(secret) if wrapped else secret)
    _assert_safe_error(caught.value, *markers)


@pytest.mark.parametrize("credential", ["zalo_bot_token", "zalo_bot_webhook_secret"])
def test_identity_cannot_equal_transport_credential_without_secret_echo(settings_type, credential):
    with pytest.raises(ValidationError) as caught:
        _settings(settings_type, zalo_bot_identity_secret=_KEY, **{credential: _KEY})
    _assert_safe_error(caught.value, _KEY)


@pytest.mark.parametrize("mapping_type", [dict, UserDict, MappingProxyType])
def test_model_level_errors_mask_identity_without_mutating_input(settings_type, mapping_type):
    values = dict(
        app_env="test", enforce_approved_llm_provider=False,
        zalo_bot_enabled=True, zalo_bot_webhook_secret="",
        zalo_bot_identity_secret=_KEY, zalo_bot_token="transport-token",
    )
    with pytest.raises(ValidationError) as caught:
        settings_type.model_validate(mapping_type(values))
    _assert_safe_error(caught.value, _KEY)
    assert values["zalo_bot_identity_secret"] == _KEY


@pytest.mark.parametrize("chat", [None, 739182640517, ["private-chat"], "", "private-chat-\ud800"])
def test_invalid_chat_inputs_fail_safely(chat):
    with pytest.raises(ValueError) as caught:
        opaque_identity(chat, _KEY)
    assert _KEY not in repr(caught.value)
    assert "private-chat" not in repr(caught.value)
    assert "739182640517" not in repr(caught.value)


@pytest.mark.parametrize("secret", [None, 739182640517, ["private-key"], "", "private-key",
                                     "private-key-" * 24, "private-key-\ud800" * 3])
def test_invalid_runtime_secrets_fail_safely(secret):
    with pytest.raises(ValueError) as caught:
        opaque_identity(_CHAT, secret)
    assert _CHAT not in repr(caught.value)
    assert "private-key" not in repr(caught.value)
    assert "739182640517" not in repr(caught.value)


@pytest.mark.parametrize("identity", [None, 739182640517, ["private-identity"], "",
                                       "private-identity-\ud800"])
def test_invalid_session_inputs_fail_safely(identity):
    with pytest.raises(ValueError) as caught:
        global_session_id(identity)
    assert "private-identity" not in repr(caught.value)
    assert "739182640517" not in repr(caught.value)
