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

from api.application.services.zalo_bot_identity import global_session_id, ingress_key, opaque_identity

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
    surfaces = [str(exc), repr(exc), "".join(traceback.format_exception(exc))]
    if isinstance(exc, ValidationError):
        surfaces.extend([exc.json(), repr(exc.errors()), json.dumps(exc.errors(), default=str)])
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
    assert ingress_key(_CHAT, "message", original.zalo_bot_identity_secret.get_secret_value()) == (
        ingress_key(_CHAT, "message", rotated.zalo_bot_identity_secret.get_secret_value())
    )
    assert isinstance(rotated.zalo_bot_token, str)


def test_missing_identity_is_inert_only_when_disabled(settings_type):
    settings = _settings(settings_type)
    assert settings.zalo_bot_identity_secret.get_secret_value() == ""
    settings.validate_zalo_runtime()
    with pytest.raises(ValueError):
        opaque_identity(_CHAT, settings.zalo_bot_identity_secret.get_secret_value())
    enabled = _settings(settings_type, zalo_bot_enabled=True)
    with pytest.raises(ValueError):
        enabled.validate_zalo_runtime()


@pytest.mark.parametrize("secret", ["x" * 32, "x" * 256, "密" * 32])
def test_configured_key_boundaries_and_masking(settings_type, secret):
    settings = _settings(settings_type, zalo_bot_enabled=True, zalo_bot_identity_secret=secret)
    settings.validate_zalo_runtime()
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
    with pytest.raises(ValueError) as caught:
        settings = _settings(settings_type, zalo_bot_enabled=True,
                             zalo_bot_identity_secret=SecretStr(secret) if wrapped else secret)
        settings.validate_zalo_runtime()
    _assert_safe_error(caught.value, *markers)


@pytest.mark.parametrize("credential", ["zalo_bot_token", "zalo_bot_webhook_secret"])
def test_identity_cannot_equal_transport_credential_without_secret_echo(settings_type, credential):
    settings = _settings(settings_type, zalo_bot_enabled=True,
                         zalo_bot_identity_secret=_KEY, **{credential: _KEY})
    with pytest.raises(ValueError) as caught:
        settings.validate_zalo_runtime()
    _assert_safe_error(caught.value, _KEY)


@pytest.mark.parametrize("mapping_type", [dict, UserDict, MappingProxyType])
def test_runtime_errors_mask_identity_without_mutating_input(settings_type, mapping_type):
    values = dict(
        app_env="test", enforce_approved_llm_provider=False,
        zalo_bot_enabled=True, zalo_bot_webhook_secret="",
        zalo_bot_identity_secret=_KEY, zalo_bot_token="transport-token",
    )
    settings = settings_type.model_validate(mapping_type(values))
    with pytest.raises(ValueError) as caught:
        settings.validate_zalo_runtime()
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


def test_ingress_uses_exact_domain_separated_json_pair():
    chat, message = ' trò chuyện:" ', '訊息,\\"1'
    prefix = "zalo-bot:ingress:v1:"
    pair = json.dumps([chat, message], ensure_ascii=False, separators=(",", ":"))
    expected = prefix + hmac.digest(_KEY.encode(), (prefix + pair).encode(), "sha256").hex()
    assert ingress_key(chat, message, _KEY) == expected
    assert ingress_key(chat, message, _KEY) != opaque_identity(pair, _KEY)
    assert ingress_key(chat, message, _KEY) != ingress_key(chat, message, _KEY + "-other")


@pytest.mark.parametrize("first,second", [
    (("a:b", "c"), ("a", "b:c")),
    (("a", "message"), ("b", "message")),
    (("a ", "message"), ("a", "message")),
    (("a", "message "), ("a", "message")),
    (("A", "message"), ("a", "message")),
    (("é", "message"), ("e\u0301", "message")),
    (("a", "é"), ("a", "e\u0301")),
])
def test_ingress_pairs_never_normalize_or_join_with_delimiters(first, second):
    assert ingress_key(*first, _KEY) != ingress_key(*second, _KEY)


@pytest.mark.parametrize("chat,message,secret", [
    (None, "message", _KEY),
    ("", "message", _KEY),
    (_CHAT, 123, _KEY),
    (_CHAT, "", _KEY),
    (_CHAT, "private-message-\ud800", _KEY),
    (_CHAT, "message", "short-secret"),
    (_CHAT, "message", "secret-\ud800" * 5),
])
def test_invalid_ingress_inputs_fail_safely(chat, message, secret):
    with pytest.raises(ValueError) as caught:
        ingress_key(chat, message, secret)
    assert _CHAT not in str(caught.value)
    assert _KEY not in str(caught.value)
    assert "private-message" not in str(caught.value)


def test_identity_and_session_contracts_remain_unchanged():
    identity = _PREFIX + hmac.digest(_KEY.encode(), (_PREFIX + _CHAT).encode(), "sha256").hex()
    session = str(uuid5(uuid5(NAMESPACE_URL, "zalo-bot:session:v1:"), identity))
    ingress_key(_CHAT, "message", _KEY)
    assert opaque_identity(_CHAT, _KEY) == identity
    assert global_session_id(identity) == session


def test_two_chat_ids_have_distinct_opaque_identity_and_session():
    """Req 7: distinct chat IDs yield distinct identity_key and session_id."""
    first = opaque_identity("chat-a", _KEY)
    second = opaque_identity("chat-b", _KEY)
    assert first != second
    assert global_session_id(first) != global_session_id(second)
    assert opaque_identity("chat-a", _KEY) == first
    assert global_session_id(first) == global_session_id(first)
