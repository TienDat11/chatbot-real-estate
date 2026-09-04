"""Unit tests for the sales provisioning CLI (secure-wave issue 4F).

Only pure parts plus MockTransport-driven Google paths are exercised: JWT
assertion claims, the password policy, the masking helper, env resolution,
EMAIL_EXISTS fallback and the Firestore-missing error. No test
touches the network or real credentials — the RSA key is generated in memory
at runtime so no usable-looking PEM literal exists in this file.
"""

from __future__ import annotations

import json

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.provision_sales_user import (
    DEFAULT_SALES_EMAIL,
    OAUTH2_TOKEN_ENDPOINT,
    SALES_ROLE_CLAIM,
    SERVICE_ACCOUNT_SCOPES,
    FirestoreDatabaseMissingError,
    ProvisionError,
    build_service_account_jwt,
    config_from_env,
    create_firestore_profile_doc,
    generate_strong_password,
    look_up_account_by_email,
    mask_secret_middle,
    run_provision,
    sign_up_or_fetch_existing,
    verify_role_claim_via_sign_in,
)


def _env_password(tag: str) -> str:
    """Sentinel env-password value for config tests, computed per tag.

    Test fixtures must not carry credential-looking string literals; every
    value here is assembled at runtime and asserts config pass-through only.
    """
    return "pw:" + tag + ":" + str(abs(hash(tag)) % 100000)


def _fake_pem_header() -> str:
    """Synthetic stand-in for a PEM header in env dicts; never a real key."""
    return "-" * 5 + "BEGIN\\n"


@pytest.fixture(scope="module")
def rsa_key_pair() -> tuple[str, str]:
    """In-memory RSA key pair as (private_pem, public_pem); never persisted."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


class TestServiceAccountJwt:
    def test_signature_verifies_against_public_key(self, rsa_key_pair):
        private_pem, public_pem = rsa_key_pair
        # Fresh iat so exp validation inside decode stays meaningful.
        assertion = build_service_account_jwt("sa@test.iam.gserviceaccount.com", private_pem)
        claims = pyjwt.decode(
            assertion,
            public_pem,
            algorithms=["RS256"],
            audience=OAUTH2_TOKEN_ENDPOINT,
        )
        assert claims["iss"] == "sa@test.iam.gserviceaccount.com"
        assert claims["aud"] == OAUTH2_TOKEN_ENDPOINT

    def test_header_and_required_claims(self, rsa_key_pair):
        private_pem, public_pem = rsa_key_pair
        assertion = build_service_account_jwt("sa@test.iam.gserviceaccount.com", private_pem)
        header = pyjwt.get_unverified_header(assertion)
        assert header["alg"] == "RS256"
        claims = pyjwt.decode(assertion, options={"verify_signature": False})
        # Both scopes must ride one space-joined string per Google's grant spec.
        scope_values = claims["scope"].split(" ")
        assert set(scope_values) == set(SERVICE_ACCOUNT_SCOPES.split(" "))
        assert claims["exp"] - claims["iat"] == 3600

    def test_accepts_env_style_escaped_newline_key(self, rsa_key_pair):
        private_pem, public_pem = rsa_key_pair
        escaped_form = private_pem.replace("\n", "\\n")  # JSON key-file / .env shape
        assertion = build_service_account_jwt("sa@test.iam.gserviceaccount.com", escaped_form)
        pyjwt.decode(assertion, public_pem, algorithms=["RS256"], audience=OAUTH2_TOKEN_ENDPOINT)


class TestPasswordPolicy:
    _LOWER = set("abcdefghijklmnopqrstuvwxyz")
    _UPPER = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    _DIGITS = set("0123456789")
    _SYMBOLS = set("!%*+-._?@^")

    def test_hits_all_four_character_classes(self):
        password = generate_strong_password(20)
        assert len(password) == 20
        assert set(password) & self._LOWER
        assert set(password) & self._UPPER
        assert set(password) & self._DIGITS
        assert set(password) & self._SYMBOLS

    def test_only_uses_allowed_alphabet(self):
        allowed = self._LOWER | self._UPPER | self._DIGITS | self._SYMBOLS
        for password in (generate_strong_password(20), generate_strong_password(32)):
            assert set(password) <= allowed

    def test_generation_is_not_deterministic(self):
        assert generate_strong_password(20) != generate_strong_password(20)

    def test_rejects_length_below_policy_minimum(self):
        with pytest.raises(ValueError):
            generate_strong_password(7)

    def test_symbols_safe_for_login_form_usage(self):
        # '#', '$', '=', quotes and whitespace break URLs, shell parsing, or
        # quoting — the generated password must stay login-form-friendly.
        forbidden = set("#$='\" \t\\;")
        assert not (self._SYMBOLS & forbidden)


class TestMaskSecretMiddle:
    def test_long_value_keeps_only_ends(self):
        assert mask_secret_middle("abcdefghijklmnop") == "abcd***mnop"

    def test_short_value_fully_masked(self):
        assert mask_secret_middle("short") == "***"
        assert mask_secret_middle("12345678") == "***"

    def test_empty_value_masked(self):
        assert mask_secret_middle("") == "***"


class TestConfigFromEnv:
    def test_applies_documented_defaults(self):
        config = config_from_env({})
        assert config.sales_email == DEFAULT_SALES_EMAIL
        assert config.sales_display_name == "Sales Demo"
        assert config.sales_temp_password is None
        assert config.sales_row_id == 6

    def test_explicit_env_overrides_win(self):
        operator_password = _env_password("provided-by-operator")
        config = config_from_env(
            {
                "SALES_EMAIL": "someone@example.com",
                "SALES_TEMP_PASSWORD": operator_password,
                "SALES_ROW_ID": "3",
                "FIREBASE_PROJECT_ID": "proj",
                "FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL": "sa@proj.iam",
                "FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY": _fake_pem_header(),
                "POSTGRES_PORT": "6543",
            }
        )
        assert config.sales_email == "someone@example.com"
        assert config.sales_temp_password == operator_password
        assert config.sales_row_id == 3
        assert config.postgres_port == 6543

    def test_require_fails_fast_listing_missing_vars(self):
        with pytest.raises(ProvisionError) as excinfo:
            config_from_env({"FIREBASE_PROJECT_ID": "proj"}).require_firebase_fields()
        message = str(excinfo.value)
        assert "FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL" in message
        assert "FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY" in message

    def test_login_password_fallback_keeps_reruns_stable(self):
        known_password = _env_password("already-known")
        config = config_from_env({"SALES_LOGIN_PASSWORD": known_password})
        assert config.sales_temp_password == known_password
        # Explicit SALES_TEMP_PASSWORD still wins over the fallback.
        explicit_password = _env_password("explicit")
        other_password = _env_password("known")
        config = config_from_env(
            {"SALES_TEMP_PASSWORD": explicit_password, "SALES_LOGIN_PASSWORD": other_password}
        )
        assert config.sales_temp_password == explicit_password


class TestProvisionPasswordRequirement:
    def test_provision_aborts_without_env_password(self):
        config = config_from_env(
            {
                "FIREBASE_PROJECT_ID": "proj",
                "FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL": "sa@proj.iam",
                "FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY": _fake_pem_header(),
            }
        )

        def unexpected(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no network call should precede the password check")

        with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
            with pytest.raises(ProvisionError, match="SALES_TEMP_PASSWORD"):
                run_provision(config, client)

    def test_provision_accepts_password_from_env(self, rsa_key_pair):
        # A real in-memory key lets the flow reach the token-exchange step, so
        # this proves the env password gate passed without any credential leak.
        private_pem, _ = rsa_key_pair
        config = config_from_env(
            {
                "SALES_TEMP_PASSWORD": _env_password("env-provided"),
                "FIREBASE_PROJECT_ID": "proj",
                "FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL": "sa@proj.iam",
                "FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY": private_pem,
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "exchange boom"}})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProvisionError, match="oauth2 token exchange failed"):
                run_provision(config, client)


def _identity_toolkit_handler(sign_up_status: int, sign_up_body: dict, lookup_body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":signUp"):
            return httpx.Response(sign_up_status, json=sign_up_body)
        if request.url.path.endswith(":lookup"):
            return httpx.Response(200, json=lookup_body)
        return httpx.Response(404, json={"error": {"message": "UNEXPECTED"}})

    return handler


class TestSignInClaimVerification:
    @staticmethod
    def _mint_id_token(role: str | None) -> str:
        # Locally-minted token: the verifier decodes WITHOUT signature checks
        # (same contract as production code), so any well-formed JWT works.
        payload = {"sub": "uid1", "aud": "proj", "iss": "https://securetoken.google.com/proj"}
        if role is not None:
            payload["role"] = role
        # RFC 7518-compliant throwaway key length keeps PyJWT warnings silent.
        return pyjwt.encode(payload, "0" * 48, algorithm="HS256")

    def test_returns_true_when_token_carries_sales_role(self):
        id_token = self._mint_id_token("sales")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith(":signInWithPassword")
            assert request.url.params["key"] == "web-key"
            return httpx.Response(200, json={"idToken": id_token})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert verify_role_claim_via_sign_in(client, "e@x.y", "pw", "web-key") is True

    def test_returns_false_for_other_role(self):
        id_token = self._mint_id_token("admin")
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"idToken": id_token})
        )
        with httpx.Client(transport=transport) as client:
            assert verify_role_claim_via_sign_in(client, "e@x.y", "pw", "web-key") is False

    def test_skips_verification_without_web_key(self):
        with httpx.Client() as client:
            assert verify_role_claim_via_sign_in(client, "e@x.y", "pw", None) is None


class TestGoogleRestPaths:
    def test_sign_up_email_exists_falls_back_to_lookup(self):
        handler = _identity_toolkit_handler(
            400,
            {"error": {"code": 400, "message": "EMAIL_EXISTS", "status": "INVALID_ARGUMENT"}},
            {"users": [{"localId": "existing_uid_123", "email": "sales@example.com"}]},
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            uid = sign_up_or_fetch_existing(
                client, "proj", "fake-token", "sales@example.com", "pw", "Sales Demo"
            )
        assert uid == "existing_uid_123"

    def test_lookup_by_email_returns_user_or_none(self):
        handler = _identity_toolkit_handler(400, {}, {"users": []})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert look_up_account_by_email(client, "proj", "t", "nope@example.com") is None

    def test_firestore_profile_404_maps_to_database_missing_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/documents/sales/some_uid")
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": 404,
                        "message": "database not found",
                        "status": "NOT_FOUND",
                    }
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(FirestoreDatabaseMissingError):
                create_firestore_profile_doc(client, "proj", "t", "some_uid", "e@x.y", "Name")

    def test_firestore_profile_success_sends_role_field(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={"name": "projects/proj/databases/(default)/documents/sales/uid1"},
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            create_firestore_profile_doc(client, "proj", "t", "uid1", "e@x.y", "Name")
        fields = captured["body"]["fields"]
        assert fields["role"]["stringValue"] == SALES_ROLE_CLAIM
        assert fields["is_active"]["booleanValue"] is True
        assert fields["firebase_uid"]["stringValue"] == "uid1"
