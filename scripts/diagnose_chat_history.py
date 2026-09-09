"""Verify the deployed chat-history schema without persisting diagnostic data."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import asyncpg

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api.application.services.sql_leg import build_dsn  # noqa: E402


async def diagnose() -> bool:
    conn = await asyncpg.connect(build_dsn())
    try:
        column_exists = await conn.fetchval(
            """SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = $1
                  AND column_name = $2
            )""",
            "chat_sessions",
            "identity_key",
        )
        if not column_exists:
            print("FAIL: chat_sessions.identity_key is missing")
            print("Remediation: apply db/migrations/2026-08-26-chat-sessions-upgrade.sql")
            return False

        async with conn.transaction():
            await conn.execute(
                """INSERT INTO chat_sessions
                    (session_id, device_id, identity_key, project_key, title, message_count)
                    VALUES ($1, $2, $3, $4, $5, 0)""",
                f"diag-{uuid.uuid4()}",
                "diag-device",
                "diag-identity",
                "diag-project",
                "diagnostic",
            )
            raise _RollbackDiagnostic
    except _RollbackDiagnostic:
        print("PASS: chat_sessions.identity_key exists and is writable")
        return True
    finally:
        await conn.close()


class _RollbackDiagnostic(Exception):
    """Control flow to force the throwaway verification transaction to roll back."""


def main() -> int:
    try:
        return 0 if asyncio.run(diagnose()) else 1
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"FAIL: chat-history schema verification unavailable ({exc.__class__.__name__})")
        print("Remediation: verify database connectivity and apply the upgrade migration")
        return 1


if __name__ == "__main__":
    sys.exit(main())
