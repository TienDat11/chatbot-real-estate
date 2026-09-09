"""Read-only gate for Firebase sales provisioning consistency.

Credentials are loaded only from environment/secret manager variables. The command
returns non-zero when active Postgres sales rows still have no Firebase UID.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from provision_sales_user import (  # noqa: E402
    FIRESTORE_REST_BASE,
    ProvisionError,
    build_service_account_jwt,
    exchange_oauth_token,
    load_env_file_into_environ,
)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


async def _active_sales_rows() -> list[dict]:
    conn = await asyncpg.connect(
        host=_env("POSTGRES_HOST") or "localhost",
        port=int(_env("POSTGRES_PORT") or "5432"),
        user=_env("POSTGRES_USER") or "ragre",
        password=os.environ.get("POSTGRES_PASSWORD") or "",
        database=_env("POSTGRES_DATABASE") or "ragre",
        timeout=10,
    )
    try:
        rows = await conn.fetch(
            "SELECT id, full_name, firebase_uid FROM sales "
            "WHERE is_active = TRUE ORDER BY id"
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


def _firebase_access_token() -> tuple[str, str] | None:
    project = _env("FIREBASE_PROJECT_ID")
    email = _env("FIREBASE_SERVICE_ACCOUNT_CLIENT_EMAIL")
    private_key = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY")
    if not project or not email or not private_key:
        return None
    assertion = build_service_account_jwt(email, private_key)
    try:
        with httpx.Client(timeout=30.0) as client:
            return project, exchange_oauth_token(client, assertion)
    except (httpx.HTTPError, ProvisionError) as exc:
        print(
            f"[FAIL] Firebase credential exchange unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        return None


def _probe_profiles(uids: list[str]) -> bool:
    unique_uids = list(dict.fromkeys(uids))
    if not unique_uids:
        return True
    auth = _firebase_access_token()
    if auth is None:
        print("[FAIL] Firebase profile probe unavailable: required FIREBASE_* env vars absent")
        return False
    project, token = auth
    healthy = True
    with httpx.Client(timeout=30.0) as client:
        for uid in unique_uids:
            response = client.get(
                f"{FIRESTORE_REST_BASE}/projects/{project}/databases/(default)/documents/sales/{uid}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 404:
                print(f"[FAIL] firestore sales/{uid} missing")
                healthy = False
                continue
            if response.status_code != 200:
                print(f"[FAIL] firestore sales/{uid} probe HTTP {response.status_code}")
                healthy = False
                continue
            fields = response.json().get("fields") or {}
            expected = {
                "firebase_uid": uid,
                "role": "sales",
                "is_active": True,
            }
            actual = {
                "firebase_uid": (fields.get("firebase_uid") or {}).get("stringValue"),
                "role": (fields.get("role") or {}).get("stringValue"),
                "is_active": (fields.get("is_active") or {}).get("booleanValue"),
            }
            missing = [key for key, value in expected.items() if actual.get(key) != value]
            if missing:
                print(f"[FAIL] firestore sales/{uid} profile fields invalid: {','.join(missing)}")
                healthy = False
            else:
                print(f"[OK] firestore sales/{uid} present and active")
    return healthy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose sales Firebase/PG provisioning")
    parser.add_argument("--dry", action="store_true", help="read-only diagnostic mode (default)")
    parser.add_argument("--uid", action="append", default=[], help="Firebase UID to probe")
    args = parser.parse_args(argv)
    load_env_file_into_environ(SCRIPT_DIR.parent / ".env")
    try:
        rows = asyncio.run(_active_sales_rows())
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"[FAIL] Postgres diagnostic unavailable: {type(exc).__name__}", file=sys.stderr)
        return 2
    unbound = [row for row in rows if row.get("firebase_uid") is None]
    print(f"[INFO] active sales rows with NULL firebase_uid: {len(unbound)}")
    for row in unbound:
        print(f"[FAIL] sales id={row['id']} is not Firebase-bound")
    mapped_uids = [str(row["firebase_uid"]) for row in rows if row.get("firebase_uid")]
    requested_uids = [str(uid) for uid in args.uid]
    known_uids = set(mapped_uids)
    for uid in requested_uids:
        if uid in known_uids:
            print(f"[OK] postgres sales mapping found for firebase uid {uid}")
        else:
            print(f"[FAIL] postgres active sales mapping missing for firebase uid {uid}")
    profiles_ok = _probe_profiles([*mapped_uids, *requested_uids])
    requested_pg_ok = all(uid in known_uids for uid in requested_uids)
    return 1 if unbound or not profiles_ok or not requested_pg_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
