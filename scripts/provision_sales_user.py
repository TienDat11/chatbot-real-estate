"""Provision one real Firebase sales account and map it to its PG sales row.

Secure-wave issue 4F backend half. Stack lock bans firebase-admin, so every
Google-side mutation rides the public REST surface with a hand-built RS256
service-account grant (same shape as the Firestore mirror adapter):

    SA JWT -> OAuth2 access token -> Identity Toolkit v1 -> Firestore REST v1

All configuration comes from the environment (optionally seeded from the
repo-root .env); no credential literal ever appears here and no secret is
ever written back to disk. The temporary password must be provided via env
(SALES_TEMP_PASSWORD or SALES_LOGIN_PASSWORD); the script never generates,
prints, or persists a credential.

Usage:
    python scripts/provision_sales_user.py --dry   # plan mutations, zero network
    python scripts/provision_sales_user.py         # provision for real

Environment:
    SALES_EMAIL                             login email (default sales.demo.ragre@gmail.com)
    SALES_DISPLAY_NAME                      profile name (default Sales Demo)
    SALES_TEMP_PASSWORD                     fixed temp password; REQUIRED for provisioning
    SALES_ROW_ID                            PG sales row to map (default 6)
    FIREBASE_PROJECT_ID                     GCP/Firebase project
    FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL   service-account identity
    FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY    PEM with \\n escapes (JSON key-file form)
    FIREBASE_WEB_API_KEY                    optional; enables sign-in-based claim verification
    POSTGRES_HOST/PORT/USER/PASSWORD/DATABASE  PG mapping target
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import string
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import jwt

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE_PATH = REPO_ROOT / ".env"

OAUTH2_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
IDENTITY_TOOLKIT_REST_BASE = "https://identitytoolkit.googleapis.com/v1"
FIRESTORE_REST_BASE = "https://firestore.googleapis.com/v1"


def _account_action_url(action: str) -> str:
    # Project-scoped /v1/projects/<pid>/accounts:* routes serve a bare
    # front-end 404 on projects without an explicit Identity Platform API
    # binding; the legacy unscoped /v1/accounts:<action> surface accepts the
    # same bearer grant and resolves the project from the credential itself.
    return f"{IDENTITY_TOOLKIT_REST_BASE}/accounts:{action}"
# cloud-platform covers the datastore.* calls; identitytoolkit covers accounts:*.
SERVICE_ACCOUNT_SCOPES = (
    "https://www.googleapis.com/auth/identitytoolkit "
    "https://www.googleapis.com/auth/cloud-platform"
)

DEFAULT_SALES_EMAIL = "sales.demo.ragre@gmail.com"
DEFAULT_SALES_DISPLAY_NAME = "Sales Demo"
SALES_ROLE_CLAIM = "sales"

# Login-form-safe symbols only: '#' gets dropped as a URL fragment, '$'/'='
# trip shell/tooling parsing, quotes and whitespace complicate quoting. Kept
# for operators generating a password to set in their env/secret store.
_PASSWORD_SYMBOLS = "!%*+-._?@^"


class ProvisionError(Exception):
    """Hard failure that should abort provisioning with a non-zero exit."""


class FirestoreDatabaseMissingError(ProvisionError):
    """Firestore DB absent (404 NOT_FOUND) — explicitly non-blocking by design."""


def mask_secret_middle(value: str) -> str:
    """Keep only the outer ends visible; short values are fully masked."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def generate_strong_password(length: int = 20) -> str:
    """Secrets-based password guaranteed to hit all four character classes."""
    if length < 8:
        raise ValueError("password length below policy minimum")
    rng = secrets.SystemRandom()
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, _PASSWORD_SYMBOLS]
    # One guaranteed pick per class, then fill from the combined pool and
    # shuffle so the guaranteed picks are not predictably placed at the front.
    password_chars = [rng.choice(pool) for pool in pools]
    combined = "".join(pools)
    password_chars.extend(rng.choice(combined) for _ in range(length - len(password_chars)))
    rng.shuffle(password_chars)
    return "".join(password_chars)


def build_service_account_jwt(
    client_email: str,
    private_key: str,
    issued_at: int | None = None,
    expires_in_seconds: int = 3600,
) -> str:
    """Sign the OAuth2 jwt-bearer assertion; env keys carry \\n escapes."""
    issued_at = issued_at if issued_at is not None else int(time.time())
    return jwt.encode(
        {
            "iss": client_email,
            "scope": SERVICE_ACCOUNT_SCOPES,
            "aud": OAUTH2_TOKEN_ENDPOINT,
            "iat": issued_at,
            "exp": issued_at + expires_in_seconds,
        },
        private_key.replace("\\n", "\n"),
        algorithm="RS256",
    )


@dataclass(frozen=True)
class ProvisionConfig:
    """Everything the CLI needs, resolved from env only."""

    sales_email: str
    sales_display_name: str
    sales_temp_password: str | None
    sales_row_id: int
    firebase_project_id: str | None
    service_account_client_email: str | None
    service_account_private_key: str | None
    # Public client identifier (config.py calls it the ops-only web key); used
    # solely for the sign-in-based claim verification, never for token verify.
    firebase_web_api_key: str | None = field(default=None)
    postgres_host: str = field(default="localhost")
    postgres_port: int = field(default=5432)
    postgres_user: str = field(default="ragre")
    postgres_password: str = field(default="ragre_dev_password")
    postgres_database: str = field(default="ragre")

    def require_firebase_fields(self) -> tuple[str, str, str]:
        """Return (project_id, client_email, private_key) or fail fast clearly."""
        missing = [
            name
            for name, value in (
                ("FIREBASE_PROJECT_ID", self.firebase_project_id),
                ("FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL", self.service_account_client_email),
                ("FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY", self.service_account_private_key),
            )
            if not value
        ]
        if missing:
            raise ProvisionError(f"missing required env vars: {', '.join(missing)}")
        assert self.firebase_project_id and self.service_account_client_email
        assert self.service_account_private_key
        return (
            self.firebase_project_id,
            self.service_account_client_email,
            self.service_account_private_key,
        )


def config_from_env(env: Mapping[str, str]) -> ProvisionConfig:
    """Pure resolver so tests can pass synthetic mappings."""
    private_key_raw = env.get("FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY") or None
    # SALES_LOGIN_PASSWORD fallback keeps re-runs stable: without it a fresh
    # random password would silently rotate the credential the user knows.
    temp_password = env.get("SALES_TEMP_PASSWORD") or env.get("SALES_LOGIN_PASSWORD") or None
    return ProvisionConfig(
        sales_email=(env.get("SALES_EMAIL") or DEFAULT_SALES_EMAIL).strip(),
        sales_display_name=(env.get("SALES_DISPLAY_NAME") or DEFAULT_SALES_DISPLAY_NAME).strip(),
        sales_temp_password=temp_password,
        sales_row_id=int(env.get("SALES_ROW_ID") or "6"),
        firebase_project_id=(env.get("FIREBASE_PROJECT_ID") or None),
        service_account_client_email=(env.get("FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL") or None),
        firebase_web_api_key=(env.get("FIREBASE_WEB_API_KEY") or None),
        # Keep the escaped form; normalization happens once at signing time.
        service_account_private_key=private_key_raw,
        postgres_host=env.get("POSTGRES_HOST") or "localhost",
        postgres_port=int(env.get("POSTGRES_PORT") or "5432"),
        postgres_user=env.get("POSTGRES_USER") or "ragre",
        postgres_password=env.get("POSTGRES_PASSWORD") or "ragre_dev_password",
        postgres_database=env.get("POSTGRES_DATABASE") or "ragre",
    )


def load_env_file_into_environ(path: Path) -> None:
    """Minimal dotenv loader (stdlib): process env wins over file values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def exchange_oauth_token(client: httpx.Client, assertion: str) -> str:
    response = client.post(
        OAUTH2_TOKEN_ENDPOINT,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
    )
    _raise_google_error(response, "oauth2 token exchange")
    return str(response.json()["access_token"])


def _google_error_message(response: httpx.Response) -> str:
    # Status and message are concatenated because some errors (missing DB)
    # carry only status="NOT_FOUND" while others carry only message text.
    try:
        payload = response.json().get("error", {})
        status = str(payload.get("status") or "")
        message = str(payload.get("message") or "")
        return f"{status} {message}".strip()
    except ValueError:
        return ""


def _raise_google_error(response: httpx.Response, action: str) -> None:
    if response.status_code >= 400:
        raise ProvisionError(
            f"{action} failed: HTTP {response.status_code}: {_google_error_message(response)}"
        )


def look_up_account_by_email(
    client: httpx.Client, project_id: str, access_token: str, email: str
) -> dict[str, Any] | None:
    response = client.post(
        _account_action_url("lookup"),
        headers={"Authorization": f"Bearer {access_token}"},
        json={"email": [email]},
    )
    _raise_google_error(response, "accounts:lookup")
    users = response.json().get("users") or []
    return users[0] if users else None


def sign_up_or_fetch_existing(
    client: httpx.Client,
    project_id: str,
    access_token: str,
    email: str,
    password: str,
    display_name: str,
) -> str:
    """Create the account, or resolve the uid when it already exists."""
    response = client.post(
        _account_action_url("signUp"),
        headers={"Authorization": f"Bearer {access_token}"},
        json={"email": email, "password": password, "displayName": display_name},
    )
    if response.status_code < 400:
        return str(response.json()["localId"])
    if "EMAIL_EXISTS" in _google_error_message(response):
        existing = look_up_account_by_email(client, project_id, access_token, email)
        if existing is None:
            raise ProvisionError(f"account reports EMAIL_EXISTS but lookup found nothing: {email}")
        print(f"[OK] account already exists, reusing uid {existing['localId']}")
        return str(existing["localId"])
    _raise_google_error(response, "accounts:signUp")
    raise ProvisionError("accounts:signUp failed without a parseable error")  # pragma: no cover


def update_account_details(
    client: httpx.Client,
    project_id: str,
    access_token: str,
    uid: str,
    role: str,
    display_name: str,
    password: str,
) -> None:
    # customClaims replaces the whole claim set; role is the only signed gate
    # the API deps read. The password reset keeps re-runs consistent with the
    # printed/persisted temp credential.
    response = client.post(
        _account_action_url("update"),
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "localId": uid,
            "displayName": display_name,
            "password": password,
            # Identity Toolkit REST expects Firebase custom claims as a JSON string
            # in customAttributes; customClaims is not a supported update field.
            "customAttributes": json.dumps({"role": role}, separators=(",", ":")),
        },
    )
    _raise_google_error(response, "accounts:update")


def _decode_token_payload_unverified(id_token: str) -> dict[str, Any]:
    # Signature is NOT checked here on purpose: the token arrived directly over
    # TLS from Google's sign-in endpoint and only feeds a CLI report line; the
    # API's JWKS verifier port stays the single authority for request gating.
    payload_part = id_token.split(".")[1]
    padded = payload_part + "=" * (-len(payload_part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def verify_role_claim_via_sign_in(
    client: httpx.Client,
    sales_email: str,
    password: str,
    web_api_key: str | None,
) -> bool | None:
    """Read the role claim from a REAL password sign-in ID token.

    Returns True/False, or None when no FIREBASE_WEB_API_KEY is configured.
    accounts:lookup is deliberately NOT used: several Identity Toolkit
    backends omit customAttributes from GetAccountInfo even when claims are
    live (observed on this project), while the minted token is the truth the
    API actually enforces.
    """
    if not web_api_key:
        return None
    response = client.post(
        _account_action_url("signInWithPassword"),
        params={"key": web_api_key},
        json={"email": sales_email, "password": password, "returnSecureToken": True},
    )
    _raise_google_error(response, "sign-in claim verification")
    claims = _decode_token_payload_unverified(str(response.json()["idToken"]))
    return claims.get("role") == SALES_ROLE_CLAIM


def delete_firebase_account(
    client: httpx.Client, project_id: str, access_token: str, uid: str
) -> None:
    """Delete exactly one Firebase account selected by the caller."""
    response = client.post(
        _account_action_url("delete"),
        headers={"Authorization": f"Bearer {access_token}"},
        json={"localId": uid},
    )
    if response.status_code == 404:
        return
    _raise_google_error(response, "accounts:delete")


def delete_firestore_profile_doc(
    client: httpx.Client, project_id: str, access_token: str, uid: str
) -> None:
    """Delete only the targeted sales profile; an absent document is success."""
    response = client.delete(
        f"{FIRESTORE_REST_BASE}/projects/{project_id}"
        f"/databases/(default)/documents/sales/{uid}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if response.status_code == 404:
        return
    _raise_google_error(response, "firestore profile delete")


def create_firestore_profile_doc(
    client: httpx.Client,
    project_id: str,
    access_token: str,
    uid: str,
    email: str,
    display_name: str,
) -> None:
    """Upsert sales/{uid}; raises FirestoreDatabaseMissingError on an absent DB."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    response = client.patch(
        f"{FIRESTORE_REST_BASE}/projects/{project_id}"
        f"/databases/(default)/documents/sales/{uid}",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "fields": {
                "firebase_uid": {"stringValue": uid},
                "email": {"stringValue": email},
                "display_name": {"stringValue": display_name},
                "role": {"stringValue": SALES_ROLE_CLAIM},
                "is_active": {"booleanValue": True},
                "provisioned_at": {"timestampValue": now_iso},
            }
        },
    )
    if response.status_code >= 400:
        message = _google_error_message(response)
        if response.status_code == 404 and "NOT_FOUND" in message.upper():
            raise FirestoreDatabaseMissingError(message)
        raise ProvisionError(
            f"firestore profile write failed: HTTP {response.status_code}: {message}"
        )


def probe_firestore_databases(
    client: httpx.Client, project_id: str, access_token: str
) -> list[dict[str, Any]]:
    response = client.get(
        f"{FIRESTORE_REST_BASE}/projects/{project_id}/databases",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    _raise_google_error(response, "firestore databases probe")
    return list(response.json().get("databases") or [])


def map_sales_firebase_uid(config: ProvisionConfig, firebase_uid: str) -> tuple[int, str, str]:
    """Store the uid on the configured sales row; returns (id, full_name, masked_key)."""
    async def _map_on_one_connection() -> tuple[int, str, str]:
        # One short-lived connection keeps the CLI free of pool lifecycle code.
        conn = await asyncpg.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            user=config.postgres_user,
            password=config.postgres_password,
            database=config.postgres_database,
            timeout=10,
        )
        try:
            rows = await conn.fetch("SELECT id, full_name, access_key FROM sales ORDER BY id")
            print("[PG] current sales rows (access_key masked):")
            for row in rows:
                print(
                    f"     id={row['id']} name={row['full_name']!r} "
                    f"access_key={mask_secret_middle(row['access_key'])}"
                )
            updated = await conn.fetchrow(
                "UPDATE sales SET firebase_uid = $1, updated_at = now() "
                "WHERE id = $2 RETURNING id, full_name, access_key",
                firebase_uid,
                config.sales_row_id,
            )
            if updated is None:
                raise ProvisionError(f"sales row id={config.sales_row_id} does not exist")
            return (
                int(updated["id"]),
                str(updated["full_name"]),
                mask_secret_middle(str(updated["access_key"])),
            )
        finally:
            await conn.close()

    try:
        return asyncio.run(_map_on_one_connection())
    except asyncpg.exceptions.UndefinedColumnError:
        raise ProvisionError(
            "column sales.firebase_uid missing — run "
            "db/migrations/2026-08-24-sales-firebase-uid.sql first"
        ) from None
    except asyncpg.exceptions.UniqueViolationError:
        raise ProvisionError(
            "another sales row already maps this firebase_uid (uq_sales_firebase_uid)"
        ) from None
    except OSError as exc:
        raise ProvisionError(
            f"cannot reach Postgres at {config.postgres_host}:{config.postgres_port}: {exc}"
        ) from exc


UNBLOCK_FIRESTORE_MESSAGE = """
[UNBLOCK] Firestore database is missing (404 NOT_FOUND).
The sales/{uid} profile could NOT be written. Everything else succeeded.
USER ACTION (pick one):
  1. Create a Firestore NATIVE-mode database (region asia-southeast1 recommended)
     in the Firebase console for this project, then re-run this script, OR
  2. Grant the service account roles/datastore.owner on the project.
This step is intentionally NON-BLOCKING: the PG mapping and the Firebase
account/claims are already live, so login works right now.
"""


def _require_single_reprovision_selector(env: Mapping[str, str]) -> str:
    """Require one, and only one, stale identity selector from env."""
    selectors = [env.get("SALES_REPROVISION_UID"), env.get("SALES_REPROVISION_EMAIL")]
    selected = [value.strip() for value in selectors if value and value.strip()]
    if len(selected) != 1:
        raise ProvisionError(
            "reprovision requires exactly one of SALES_REPROVISION_UID or "
            "SALES_REPROVISION_EMAIL"
        )
    return selected[0]


def run_reprovision(config: ProvisionConfig, client: httpx.Client, env: Mapping[str, str]) -> int:
    """Replace one stale identity, never enumerate or mutate a shared account."""
    selector = _require_single_reprovision_selector(env)
    project_id, client_email, private_key = config.require_firebase_fields()
    if not config.sales_temp_password:
        raise ProvisionError("SALES_TEMP_PASSWORD (or SALES_LOGIN_PASSWORD) must be set")
    assertion = build_service_account_jwt(client_email, private_key)
    access_token = exchange_oauth_token(client, assertion)
    uid_selector = bool((env.get("SALES_REPROVISION_UID") or "").strip())
    account = (
        {"localId": selector}
        if uid_selector
        else look_up_account_by_email(client, project_id, access_token, selector)
    )
    if account is None:
        raise ProvisionError("reprovision target was not found")
    old_uid = str(account["localId"])
    delete_firebase_account(client, project_id, access_token, old_uid)
    delete_firestore_profile_doc(client, project_id, access_token, old_uid)
    new_uid = sign_up_or_fetch_existing(
        client, project_id, access_token, config.sales_email,
        config.sales_temp_password, config.sales_display_name,
    )
    update_account_details(
        client, project_id, access_token, new_uid, SALES_ROLE_CLAIM,
        config.sales_display_name, config.sales_temp_password,
    )
    create_firestore_profile_doc(
        client, project_id, access_token, new_uid, config.sales_email, config.sales_display_name
    )
    map_sales_firebase_uid(config, new_uid)
    print(f"[OK] reprovisioned one target; old uid={old_uid}, new uid={new_uid}")
    return 0


def run_dry(
    config: ProvisionConfig,
    *,
    reprovision: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    print("[DRY] planned mutations (no network, no DB writes performed):")
    if reprovision:
        selector = _require_single_reprovision_selector(env or os.environ)
        print(f"  0. accounts:delete exactly one selected target ({selector[:3]}***)")
    password_source = (
        "SALES_TEMP_PASSWORD from env"
        if config.sales_temp_password
        else "MISSING — provisioning will abort (set SALES_TEMP_PASSWORD)"
    )
    print(f"  1. Identity Toolkit accounts:signUp email={config.sales_email} "
          f"display={config.sales_display_name!r} (or reuse existing uid)")
    print(
        f"  2. accounts:update customClaims={{\"role\": \"{SALES_ROLE_CLAIM}\"}} "
        f"+ password reset ({password_source})"
    )
    print("  3. Firestore PATCH sales/<uid-from-step-1> profile document")
    print(f"  4. Postgres UPDATE sales SET firebase_uid=<uid> WHERE id={config.sales_row_id}")
    print("  5. No secrets written to disk; credentials must come from env/secret service")
    firebase_ready = bool(
        config.firebase_project_id
        and config.service_account_client_email
        and config.service_account_private_key
    )
    print(f"[DRY] firebase credentials present: {firebase_ready}")
    print(
        f"[DRY] postgres target: {config.postgres_host}:{config.postgres_port}/"
        f"{config.postgres_database}"
    )


def run_provision(config: ProvisionConfig, client: httpx.Client) -> int:
    project_id, client_email, private_key = config.require_firebase_fields()

    temp_password = config.sales_temp_password
    if temp_password is None:
        raise ProvisionError(
            "SALES_TEMP_PASSWORD (or SALES_LOGIN_PASSWORD) must be set via "
            "environment or a secret service; the script never generates, "
            "prints, or persists a credential"
        )

    print(f"[AUTH] signing service-account assertion for {client_email}")
    assertion = build_service_account_jwt(client_email, private_key)
    access_token = exchange_oauth_token(client, assertion)
    print("[AUTH] OAuth2 access token acquired")

    uid = sign_up_or_fetch_existing(
        client,
        project_id,
        access_token,
        config.sales_email,
        temp_password,
        config.sales_display_name,
    )
    print(f"[OK] firebase uid: {uid}")

    update_account_details(
        client,
        project_id,
        access_token,
        uid,
        SALES_ROLE_CLAIM,
        config.sales_display_name,
        temp_password,
    )
    print(f"[OK] custom claim role={SALES_ROLE_CLAIM} written")

    claim_verified = verify_role_claim_via_sign_in(
        client, config.sales_email, temp_password, config.firebase_web_api_key
    )
    if claim_verified is True:
        print("[OK] claim verified in fresh sign-in ID token as role=sales")
    elif claim_verified is None:
        print("[WARN] FIREBASE_WEB_API_KEY absent — sign-in claim verification skipped")
    else:
        raise ProvisionError("sign-in succeeded but the ID token does not carry role=sales")

    sales_id, full_name, masked_access_key = map_sales_firebase_uid(config, uid)
    print(f"[OK] mapped sales row id={sales_id} name={full_name!r} access_key={masked_access_key}")

    exit_code = 0
    try:
        create_firestore_profile_doc(
            client,
            project_id,
            access_token,
            uid,
            config.sales_email,
            config.sales_display_name,
        )
        print(f"[OK] firestore profile sales/{uid} created")
    except FirestoreDatabaseMissingError:
        print(UNBLOCK_FIRESTORE_MESSAGE.replace("{uid}", uid))
        exit_code = 0  # non-blocking by design

    databases = probe_firestore_databases(client, project_id, access_token)
    if databases:
        for database in databases:
            print(
                f"[PROBE] firestore database present: name={database.get('name')} "
                f"type={database.get('type')} location={database.get('locationId')}"
            )
    else:
        print("[PROBE] no Firestore database exists yet for this project")

    print("[OK] no credentials written to disk; SALES_TEMP_PASSWORD stays in env/secret service")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision one Firebase sales account + PG mapping."
    )
    parser.add_argument(
        "--dry", action="store_true", help="print planned mutations with zero network calls"
    )
    parser.add_argument(
        "--reprovision", action="store_true", help="replace exactly one env-selected identity"
    )
    parser.add_argument(
        "--confirm", action="store_true", help="allow the explicitly selected live mutation"
    )
    args = parser.parse_args(argv)

    load_env_file_into_environ(ENV_FILE_PATH)
    config = config_from_env(os.environ)

    if args.dry:
        run_dry(config, reprovision=args.reprovision, env=os.environ)
        return 0
    if args.reprovision and not args.confirm:
        print("[FAIL] --reprovision requires --confirm; run --dry first", file=sys.stderr)
        return 2
    try:
        with httpx.Client(timeout=30.0) as client:
            if args.reprovision:
                return run_reprovision(config, client, os.environ)
            return run_provision(config, client)
    except FirestoreDatabaseMissingError:
        # Already handled inline; kept for safety if raised from a future path.
        return 0
    except ProvisionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
