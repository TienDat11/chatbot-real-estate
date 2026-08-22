"""Story 8.3 / ISSUE-06 — admin/sales/viewer auth dependencies (offline).

Same local-RSA JWKS pattern as tests/test_firebase_realtime_base.py: tokens are
minted with a locally generated RSA key, the verifier's key resolution is
monkeypatched, and the PG sales mapping is a fake — nothing touches the
network or a real database. Covers the role claim matrix per dependency, the
401 family (expired / wrong audience / bad signature / missing header), the
403 for a missing role claim, and the sales_id attachment through the real
main.py routes.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from api.infrastructure import dependencies as dependency_injection
from api.infrastructure.adapters import firebase_auth_jwks
from api.interfaces.api import deps as admin_deps
from api.interfaces.api.deps import (
    AuthenticatedPrincipal,
    require_admin,
    require_sales,
    require_viewer,
)
from api.interfaces.api.main import create_app

PROJECT_ID = "sale-chat-bot-11e49"
ISSUER = f"https://securetoken.google.com/{PROJECT_ID}"

# Fixed uid -> sales_id rows the fake mapping "returns from PG".
MAPPED_SALES_ID_BY_UID = {"uid-sales-mapped": 777}


@pytest.fixture(scope="module")
def local_rsa_jwk() -> dict:
    """One module-scoped RSA key pair exported as a JWK entry (kid pinned)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk_entry = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk_entry["kid"] = "local-test-key"
    jwk_entry["_private_key"] = private_key
    return jwk_entry


@pytest.fixture(scope="module")
def foreign_rsa_private_key():
    """A second key pair: tokens signed by it must fail verification."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _build_offline_verifier(local_rsa_jwk: dict) -> firebase_auth_jwks.FirebaseAuthJwksVerifier:
    """Verifier whose JWKS key resolution is the local key (never HTTP)."""
    async def fake_key_for_kid(kid: str):
        if kid != local_rsa_jwk["kid"]:
            return None
        jwk_entry = {k: v for k, v in local_rsa_jwk.items() if not k.startswith("_")}
        return RSAAlgorithm.from_jwk(jwk_entry)

    verifier_instance = firebase_auth_jwks.FirebaseAuthJwksVerifier(
        project_id=PROJECT_ID,
        jwks_url="https://example.invalid/jwks",
        issuer=ISSUER,
        audience=PROJECT_ID,
    )
    verifier_instance._key_for_kid = fake_key_for_kid  # type: ignore[method-assign]
    return verifier_instance


def _mint_id_token(
    local_rsa_jwk: dict,
    claims: dict,
    *,
    signing_key=None,
    kid: str | None = None,
) -> str:
    """Sign claims RS256 with the local (or an explicitly foreign) key."""
    return jwt.encode(
        claims,
        key=signing_key or local_rsa_jwk["_private_key"],
        algorithm="RS256",
        headers={"kid": kid or local_rsa_jwk["kid"]},
    )


def _base_claims(firebase_uid: str, role: str | None, **overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": PROJECT_ID,
        "sub": firebase_uid,
        "user_id": firebase_uid,
        "email": f"{firebase_uid}@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
        "firebase": {"sign_in_provider": "password"},
    }
    if role is not None:
        claims["role"] = role
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=True)
def offline_auth_seams(monkeypatch: pytest.MonkeyPatch, local_rsa_jwk: dict) -> None:
    """Point the DI verifier provider and the sales mapping at local fakes."""
    verifier_instance = _build_offline_verifier(local_rsa_jwk)
    monkeypatch.setattr(
        dependency_injection,
        "get_firebase_auth_verifier",
        lambda: verifier_instance,
    )

    def fake_fetch_active_sales_id_sync(firebase_uid: str) -> int | None:
        return MAPPED_SALES_ID_BY_UID.get(firebase_uid)

    monkeypatch.setattr(
        admin_deps.admin,
        "_fetch_active_sales_id_sync",
        fake_fetch_active_sales_id_sync,
    )


@pytest.fixture(scope="module")
def role_matrix_app() -> FastAPI:
    """Minimal app mounting all three dependencies (require_viewer has no
    production route yet, so the claim matrix is exercised here)."""
    app = FastAPI()

    def _principal_payload(authenticated_principal: AuthenticatedPrincipal) -> dict:
        return {
            "firebase_uid": authenticated_principal.firebase_uid,
            "email": authenticated_principal.email,
            "role": authenticated_principal.role,
            "sales_id": authenticated_principal.sales_id,
        }

    @app.get("/dependency/admin")
    async def admin_endpoint(
        authenticated_principal: AuthenticatedPrincipal = Depends(require_admin),
    ) -> dict:
        return _principal_payload(authenticated_principal)

    @app.get("/dependency/sales")
    async def sales_endpoint(
        authenticated_principal: AuthenticatedPrincipal = Depends(require_sales),
    ) -> dict:
        return _principal_payload(authenticated_principal)

    @app.get("/dependency/viewer")
    async def viewer_endpoint(
        authenticated_principal: AuthenticatedPrincipal = Depends(require_viewer),
    ) -> dict:
        return _principal_payload(authenticated_principal)

    return app


DEPENDENCY_PATH_BY_ROLE = {
    "admin": "/dependency/admin",
    "sales": "/dependency/sales",
    "viewer": "/dependency/viewer",
}


def _bearer_headers(id_token: str) -> dict:
    return {"Authorization": f"Bearer {id_token}"}


# ---------------------------------------------------------------------------
# Role claim matrix: each dependency allows exactly its own role claim.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("principal_role", ["admin", "sales", "viewer"])
@pytest.mark.parametrize("dependency_role", ["admin", "sales", "viewer"])
def test_role_claim_matrix_allowed_vs_denied_per_dependency(
    role_matrix_app: FastAPI,
    local_rsa_jwk: dict,
    dependency_role: str,
    principal_role: str,
) -> None:
    token = _mint_id_token(
        local_rsa_jwk, _base_claims(f"uid-{principal_role}", principal_role)
    )
    response = TestClient(role_matrix_app).get(
        DEPENDENCY_PATH_BY_ROLE[dependency_role], headers=_bearer_headers(token)
    )
    if principal_role == dependency_role:
        assert response.status_code == 200, response.text
        assert response.json() == {
            "firebase_uid": f"uid-{principal_role}",
            "email": f"uid-{principal_role}@example.com",
            "role": principal_role,
            "sales_id": MAPPED_SALES_ID_BY_UID.get(f"uid-{principal_role}"),
        }
    else:
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("dependency_role", ["admin", "sales", "viewer"])
def test_missing_role_claim_is_forbidden(
    role_matrix_app: FastAPI, local_rsa_jwk: dict, dependency_role: str
) -> None:
    """Authenticated but no role claim: 403, not 401 — identity is valid."""
    token = _mint_id_token(local_rsa_jwk, _base_claims("uid-roleless", None))
    response = TestClient(role_matrix_app).get(
        DEPENDENCY_PATH_BY_ROLE[dependency_role], headers=_bearer_headers(token)
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 401 family: verification failures, before any role logic runs.
# ---------------------------------------------------------------------------


def test_expired_token_is_unauthorized(role_matrix_app: FastAPI, local_rsa_jwk: dict) -> None:
    now = int(time.time())
    token = _mint_id_token(
        local_rsa_jwk,
        _base_claims("uid-admin", "admin", iat=now - 7200, exp=now - 3600),
    )
    response = TestClient(role_matrix_app).get(
        "/dependency/admin", headers=_bearer_headers(token)
    )
    assert response.status_code == 401


def test_wrong_audience_token_is_unauthorized(
    role_matrix_app: FastAPI, local_rsa_jwk: dict
) -> None:
    token = _mint_id_token(
        local_rsa_jwk, _base_claims("uid-admin", "admin", aud="other-project")
    )
    response = TestClient(role_matrix_app).get(
        "/dependency/admin", headers=_bearer_headers(token)
    )
    assert response.status_code == 401


def test_bad_signature_token_is_unauthorized(
    role_matrix_app: FastAPI, local_rsa_jwk: dict, foreign_rsa_private_key
) -> None:
    # Same kid, signed by a foreign key: the verifier resolves the local
    # public key and the RS256 check must fail.
    token = _mint_id_token(
        local_rsa_jwk,
        _base_claims("uid-admin", "admin"),
        signing_key=foreign_rsa_private_key,
    )
    response = TestClient(role_matrix_app).get(
        "/dependency/admin", headers=_bearer_headers(token)
    )
    assert response.status_code == 401


def test_missing_authorization_header_is_unauthorized(
    role_matrix_app: FastAPI,
) -> None:
    response = TestClient(role_matrix_app).get("/dependency/admin")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_garbage_token_is_unauthorized(role_matrix_app: FastAPI) -> None:
    response = TestClient(role_matrix_app).get(
        "/dependency/admin", headers=_bearer_headers("not-a-jwt")
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Real main.py routes: wiring, cross-route denial, and the sales mapping.
# ---------------------------------------------------------------------------


def test_admin_session_route_echoes_principal_from_main_app(
    local_rsa_jwk: dict,
) -> None:
    token = _mint_id_token(local_rsa_jwk, _base_claims("uid-admin", "admin"))
    response = TestClient(create_app()).get(
        "/api/admin/session", headers=_bearer_headers(token)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "firebase_uid": "uid-admin",
        "email": "uid-admin@example.com",
        "role": "admin",
        "sales_id": None,  # uid-admin has no mapped active sales row
    }


def test_admin_token_is_forbidden_on_sales_session_route(
    local_rsa_jwk: dict,
) -> None:
    token = _mint_id_token(local_rsa_jwk, _base_claims("uid-admin", "admin"))
    response = TestClient(create_app()).get(
        "/api/sales/session", headers=_bearer_headers(token)
    )
    assert response.status_code == 403


def test_sales_session_route_attaches_sales_id_from_mapping(
    local_rsa_jwk: dict,
) -> None:
    token = _mint_id_token(
        local_rsa_jwk, _base_claims("uid-sales-mapped", "sales")
    )
    response = TestClient(create_app()).get(
        "/api/sales/session", headers=_bearer_headers(token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["firebase_uid"] == "uid-sales-mapped"
    assert body["role"] == "sales"
    assert body["sales_id"] == MAPPED_SALES_ID_BY_UID["uid-sales-mapped"]


def test_sales_session_route_allows_unmapped_sales_row(
    local_rsa_jwk: dict,
) -> None:
    """A verified sales claim stays authorized even without a PG sales row:
    the mapping enriches the principal, it does not gate access."""
    token = _mint_id_token(
        local_rsa_jwk, _base_claims("uid-sales-unmapped", "sales")
    )
    response = TestClient(create_app()).get(
        "/api/sales/session", headers=_bearer_headers(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["sales_id"] is None
