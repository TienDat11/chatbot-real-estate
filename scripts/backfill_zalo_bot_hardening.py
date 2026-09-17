"""Offline ZB-03 backfill. No credentials in argv, SQL, errors or logs."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.application.services.zalo_bot_identity import ingress_key, opaque_identity
from api.infrastructure.adapters.postgres_zalo_events import (
    ZaloStorageError, _decode_outcome, _transaction, _validate_schema,
)
from api.infrastructure.adapters.postgres_leads import close_lead_pool, get_lead_pool
from api.infrastructure.config.config import get_settings


async def backfill(pool, identity_secret: str) -> int:
    """Return changed rows; reruns validate all mappings without new start receipts."""
    if get_settings().zalo_bot_enabled:
        raise ZaloStorageError("ZALO_MUST_BE_DISABLED")
    count = 0
    last_id = 0
    try:
        async with _transaction(pool) as conn:
            await _validate_schema(conn, finalized=False, indexes=False, check_data=False)
        while True:
            changed = 0
            async with _transaction(pool) as conn:
                rows = await conn.fetch(
                    "SELECT * FROM zalo_bot_events WHERE id>$1 ORDER BY id LIMIT 100 FOR UPDATE",
                    last_id,
                )
                for row in rows:
                    source = f"zalo-bot:event:v1:{row['id']}"
                    identity = opaque_identity(row["chat_id"], identity_secret)
                    key = ingress_key(row["chat_id"], row["message_id"], identity_secret)
                    if (row["status"] not in {"pending", "processing", "failed", "done", "dead", "rejected"}
                            or row["source_event_key"] not in (None, source)
                            or row["identity_key"] not in (None, identity)
                            or row["ingress_key"] not in (None, key)
                            or row["attempt_count"] < 0):
                        raise ZaloStorageError("BACKFILL_CONFLICT")
                    _decode_outcome(row["assistant_outcome"])
                    if row["outbound_message_id"] is not None and (
                            not isinstance(row["outbound_message_id"], str)
                            or not row["outbound_message_id"].strip()):
                        raise ZaloStorageError("INVALID_PROVIDER_ID")
                    receipt = await conn.fetchrow(
                        "SELECT event_id,source_event_key FROM zalo_bot_ingress_keys WHERE ingress_key=$1", key,
                    )
                    already_mapped = row["ingress_key"] is not None
                    if already_mapped and receipt is None:
                        raise ZaloStorageError("BACKFILL_CONFLICT")
                    if not already_mapped:
                        await conn.execute(
                            """INSERT INTO zalo_bot_ingress_keys (ingress_key,event_id,source_event_key,accepted_at)
                            VALUES ($1,$2,$3,$4) ON CONFLICT (ingress_key) DO NOTHING""",
                            key, row["id"], source, row["received_at"],
                        )
                        receipt = await conn.fetchrow(
                            "SELECT event_id,source_event_key FROM zalo_bot_ingress_keys WHERE ingress_key=$1", key,
                        )
                    if receipt is None or receipt["event_id"] != row["id"] or receipt["source_event_key"] != source:
                        raise ZaloStorageError("BACKFILL_CONFLICT")
                    possible_core = (row["source_event_key"] is not None
                                     or row["assistant_outcome"] is not None
                                     or row["outbound_message_id"] is not None)
                    if possible_core and not already_mapped:
                        await conn.execute(
                            """INSERT INTO zalo_bot_admissions (source_event_key,identity_key,decision)
                            VALUES ($1,$2,'started') ON CONFLICT (source_event_key) DO NOTHING""",
                            source, identity,
                        )
                    admission = await conn.fetchrow(
                        "SELECT identity_key,decision FROM zalo_bot_admissions WHERE source_event_key=$1", source,
                    )
                    if admission is not None and (admission["identity_key"] != identity
                            or admission["decision"] not in {"started", "braked"}):
                        raise ZaloStorageError("BACKFILL_CONFLICT")
                    if (row["assistant_outcome"] is not None or row["outbound_message_id"] is not None) and admission is None:
                        raise ZaloStorageError("BACKFILL_CONFLICT")
                    needs_update = (not already_mapped or row["identity_key"] is None
                                    or row["source_event_key"] is None
                                    or (row["status"] in {"done", "dead", "rejected"} and row["completed_at"] is None))
                    if needs_update:
                        await conn.execute(
                            """UPDATE zalo_bot_events SET ingress_key=$2,identity_key=$3,source_event_key=$4,
                            completed_at=CASE WHEN status IN ('done','dead','rejected')
                              THEN COALESCE(completed_at,updated_at,received_at) ELSE completed_at END
                            WHERE id=$1""", row["id"], key, identity, source,
                        )
                        changed += 1
            count += changed
            if not rows:
                break
            last_id = rows[-1]["id"]
        async with _transaction(pool) as conn:
            await _validate_schema(conn, finalized=False, indexes=False)
        return count
    except Exception as exc:
        code = exc.code if isinstance(exc, ZaloStorageError) else "BACKFILL_UNAVAILABLE"
    error = ZaloStorageError(code)
    error.completed_count = count
    raise error


async def _run(database):
    settings = get_settings()
    if settings.zalo_bot_enabled:
        raise ZaloStorageError("ZALO_MUST_BE_DISABLED")
    pool = await get_lead_pool()
    try:
        async with _transaction(pool) as conn:
            if await conn.fetchval("SELECT current_database()") != database:
                raise ZaloStorageError("DATABASE_TARGET_MISMATCH")
        return await backfill(pool, settings.zalo_bot_identity_secret.get_secret_value())
    finally:
        await close_lead_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline approved Zalo backfill; all writers must be stopped")
    parser.add_argument("--database", required=True, help="Exact configured database name")
    parser.add_argument("--approve", action="store_true", help="Approve changes to named database")
    parser.add_argument("--writers-stopped", action="store_true", help="Attest all Zalo writers/workers drained")
    args = parser.parse_args()
    if not args.approve or not args.writers_stopped:
        parser.error("Explicit approval and stopped-writer attestation required")
    try:
        count = asyncio.run(_run(args.database))
    except Exception as exc:
        code = exc.code if isinstance(exc, ZaloStorageError) else "BACKFILL_UNAVAILABLE"
        print(f"code={code} changed={getattr(exc, 'completed_count', 0)}")
        return 1
    print(f"changed={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
