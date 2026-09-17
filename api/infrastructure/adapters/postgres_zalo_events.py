"""Durable Zalo transport, permanent ingress dedupe and token-fenced leases."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import asyncpg

from api.application.services.zalo_bot_identity import ingress_key, opaque_identity
from api.infrastructure.adapters import postgres_zalo_bindings
from api.infrastructure.adapters.postgres_leads import get_lead_pool
from api.infrastructure.config.config import get_settings

if TYPE_CHECKING:
    from api.application.services.zalo_bot_processor import ZaloEvent

_DB_TIMEOUT = 5.0
_STATUSES = {"pending", "processing", "failed", "done", "dead", "rejected"}
_TERMINAL = {"done", "dead", "rejected"}


class ZaloStorageError(RuntimeError):
    """Fixed storage code only; never carry driver error data."""

    def __init__(self, code="STORAGE_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


class _LostClaim(Exception):
    """Rollback both ownership transitions, not a committed partial success."""


@asynccontextmanager
async def _transaction(pool=None):
    # Deadline includes pool checkout, SQL, commit acknowledgement and check-in.
    try:
        async with asyncio.timeout(_DB_TIMEOUT):
            pool = pool if pool is not None else await get_lead_pool()
            async with pool.acquire() as conn:
                async with conn.transaction(isolation="read_committed"):
                    await conn.execute("SET LOCAL lock_timeout = '100ms'")
                    await conn.execute("SET LOCAL statement_timeout = '5s'")
                    yield conn
    except (_LostClaim, ZaloStorageError, asyncpg.UniqueViolationError):
        raise
    except Exception:
        pass
    else:
        return
    raise ZaloStorageError() from None


def _decode_outcome(value):
    if value is None:
        return None
    try:
        value = json.loads(value) if isinstance(value, str) else value
        if (not isinstance(value, dict)
                or set(value) != {"answer", "resolution_status", "project_key", "meta"}
                or not isinstance(value["answer"], str)
                or not value["answer"].strip() or len(value["answer"]) > 2000
                or not isinstance(value["resolution_status"], str)
                or not value["resolution_status"]
                or (value["project_key"] is not None and not isinstance(value["project_key"], str))
                or value["meta"] != {}):
            raise ValueError
        value["answer"].encode("utf-8")
        return value
    except (TypeError, ValueError, UnicodeError):
        pass
    raise ZaloStorageError("INVALID_CACHE")


async def insert_event(*, chat_id: str, message_id: str, event_name: str, text: str) -> int | None:
    """Commit tombstone and transport together; only tombstone conflict is duplicate."""
    try:
        settings = get_settings()
        if not settings.zalo_bot_enabled:
            raise ZaloStorageError("ZALO_DISABLED")
        secret = settings.zalo_bot_identity_secret.get_secret_value()
        key = ingress_key(chat_id, message_id, secret)
        identity = opaque_identity(chat_id, secret)
        async with _transaction() as conn:
            # Ingress must fail closed even if startup failed or DDL is incomplete.
            await _validate_schema(conn, check_data=False)
            event_id = await conn.fetchval(
                "SELECT nextval(pg_get_serial_sequence('public.zalo_bot_events', 'id'))"
            )
            source = f"zalo-bot:event:v1:{event_id}"
            row = await conn.fetchrow(
                """INSERT INTO zalo_bot_ingress_keys (ingress_key,event_id,source_event_key)
                VALUES ($1,$2,$3) ON CONFLICT (ingress_key) DO NOTHING RETURNING event_id""",
                key, event_id, source,
            )
            if row is None:
                return None
            await conn.execute(
                """INSERT INTO zalo_bot_events
                (id,chat_id,message_id,event_name,text,ingress_key,identity_key,source_event_key)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                event_id, chat_id, message_id, event_name, text, key, identity, source,
            )
        return event_id
    except ZaloStorageError as exc:
        code = exc.code
    except Exception:
        code = "STORAGE_UNAVAILABLE"
    raise ZaloStorageError(code) from None


class PostgresZaloEventStore:
    def __init__(self, *, settings=None):
        self._settings = settings if settings is not None else get_settings()

    async def authorize(self, identity: str) -> int | None:
        return await postgres_zalo_bindings.authorize(identity)

    async def validate_schema(self) -> None:
        async with _transaction() as conn:
            await _validate_schema(conn)

    async def due_candidates(self) -> list[int]:
        if not self._settings.zalo_bot_enabled:
            return []
        async with _transaction() as conn:
            rows = await conn.fetch(
                """SELECT id FROM zalo_bot_events
                WHERE attempt_count < $1 AND received_at > now()-make_interval(hours => $2)
                  AND ((status IN ('pending','failed') AND next_attempt_at <= now())
                    OR (status='processing' AND lease_until <= now()))
                ORDER BY received_at,id LIMIT $3""",
                self._settings.zalo_bot_max_attempts,
                self._settings.zalo_bot_retry_max_age_hours,
                self._settings.zalo_bot_batch_size,
            )
        return [row["id"] for row in rows]

    async def claim_event(self, event_id: int) -> ZaloEvent | None:
        from api.application.services.zalo_bot_processor import ZaloEvent

        if not self._settings.zalo_bot_enabled:
            return None
        started = monotonic()
        token = uuid4()
        try:
            async with _transaction() as conn:
                identity = await conn.fetchval(
                    "SELECT identity_key FROM zalo_bot_events WHERE id=$1", event_id
                )
                if identity is None:
                    raise _LostClaim
                actor = await conn.fetchrow(
                    """INSERT INTO zalo_bot_actor_leases (identity_key,holder_token,lease_until)
                    VALUES ($1,$2,clock_timestamp()+make_interval(secs => $3))
                    ON CONFLICT (identity_key) DO UPDATE SET holder_token=EXCLUDED.holder_token,
                      lease_until=clock_timestamp()+make_interval(secs => $3)
                    WHERE zalo_bot_actor_leases.lease_until <= clock_timestamp()
                    RETURNING holder_token""",
                    identity, token, self._settings.zalo_bot_lease_seconds,
                )
                if actor is None:
                    raise _LostClaim
                row = await conn.fetchrow(
                    """WITH candidate AS (
                      SELECT id FROM zalo_bot_events
                      WHERE id=$5 AND identity_key=$6 AND attempt_count < $1
                        AND received_at > now()-make_interval(hours => $2)
                        AND ((status IN ('pending','failed') AND next_attempt_at <= now())
                          OR (status='processing' AND lease_until <= now()))
                      ORDER BY received_at,id FOR UPDATE SKIP LOCKED LIMIT 1
                    ) UPDATE zalo_bot_events e SET status='processing',
                      attempt_count=e.attempt_count+1, claim_token=$3, locked_at=now(),
                      lease_until=now()+make_interval(secs => $4),updated_at=now()
                    FROM candidate c WHERE e.id=c.id RETURNING e.*""",
                    self._settings.zalo_bot_max_attempts,
                    self._settings.zalo_bot_retry_max_age_hours, token,
                    self._settings.zalo_bot_lease_seconds, event_id, identity,
                )
                if row is None:
                    raise _LostClaim
                tombstone = await conn.fetchrow(
                    "SELECT event_id,source_event_key FROM zalo_bot_ingress_keys WHERE ingress_key=$1",
                    row["ingress_key"],
                )
                if (tombstone is None or tombstone["event_id"] != event_id
                        or tombstone["source_event_key"] != row["source_event_key"]
                        or row["source_event_key"] != f"zalo-bot:event:v1:{event_id}"):
                    raise ZaloStorageError("IDENTITY_CONFLICT")
                remaining = await _remaining(conn, event_id, identity, token)
            values = {name: row[name] for name in (
                "id", "chat_id", "text", "status", "identity_key", "source_event_key",
                "assistant_outcome", "outbound_message_id", "ingress_key", "claim_token", "attempt_count",
            )}
            # Processor validates cache after authorization; corruption is not an uncached miss.
            outcome = values["assistant_outcome"]
            if isinstance(outcome, str):
                try:
                    values["assistant_outcome"] = json.loads(outcome)
                except ValueError:
                    pass
            values["lease_remaining"] = max(0.0, float(remaining or 0) - (monotonic() - started))
            return ZaloEvent(**values)
        except _LostClaim:
            return None
        except ZaloStorageError:
            raise
        except Exception:
            pass
        raise ZaloStorageError()

    async def check_ownership(self, event: ZaloEvent, *, minimum_remaining=0) -> float | None:
        # F7: local monotonic elapsed time cannot observe a forward DB clock jump.
        started = monotonic()
        try:
            async with _transaction() as conn:
                remaining = await _remaining(conn, event.id, event.identity_key, event.claim_token, minimum_remaining)
            remaining = float(remaining) - (monotonic() - started) if remaining is not None else 0
            return remaining if remaining > minimum_remaining else None
        except ZaloStorageError:
            return None

    async def renew_lease(self, event: ZaloEvent) -> float | None:
        started = monotonic()
        try:
            async with _transaction() as conn:
                actor = await conn.fetchrow(
                    """UPDATE zalo_bot_actor_leases
                    SET lease_until=clock_timestamp()+make_interval(secs => $3)
                    WHERE identity_key=$1 AND holder_token=$2 AND lease_until > clock_timestamp()
                    RETURNING holder_token""",
                    event.identity_key, event.claim_token, self._settings.zalo_bot_lease_seconds,
                )
                if actor is None:
                    raise _LostClaim
                row = await conn.fetchrow(
                    """UPDATE zalo_bot_events
                    SET lease_until=clock_timestamp()+make_interval(secs => $3),updated_at=clock_timestamp()
                    WHERE id=$4 AND identity_key=$1 AND claim_token=$2
                      AND status='processing' AND lease_until > clock_timestamp()
                    RETURNING id""",
                    event.identity_key, event.claim_token, self._settings.zalo_bot_lease_seconds, event.id,
                )
                if row is None:
                    raise _LostClaim
                remaining = await _remaining(conn, event.id, event.identity_key, event.claim_token)
                if remaining is None:
                    raise _LostClaim
            remaining = float(remaining) - (monotonic() - started)
            return remaining if remaining > 0 else None
        except (_LostClaim, ZaloStorageError):
            return None

    async def _mutate(self, event, assignment, *args, release=False) -> bool:
        try:
            async with _transaction() as conn:
                actor = await conn.fetchrow(
                    """SELECT holder_token FROM zalo_bot_actor_leases
                    WHERE identity_key=$1 AND holder_token=$2 AND lease_until > clock_timestamp()
                    FOR UPDATE""", event.identity_key, event.claim_token,
                )
                if actor is None:
                    raise _LostClaim
                row = await conn.fetchrow(
                    f"""UPDATE zalo_bot_events SET {assignment},updated_at=clock_timestamp()
                    WHERE id=$1 AND identity_key=$2 AND claim_token=$3 AND status='processing'
                      AND lease_until > clock_timestamp()
                      AND EXISTS (SELECT 1 FROM zalo_bot_actor_leases a
                        WHERE a.identity_key=$2 AND a.holder_token=$3
                          AND a.lease_until > clock_timestamp()) RETURNING id""",
                    event.id, event.identity_key, event.claim_token, *args,
                )
                if row is None:
                    raise _LostClaim
                if release:
                    actor = await conn.fetchrow(
                        """UPDATE zalo_bot_actor_leases SET lease_until=clock_timestamp()
                        WHERE identity_key=$1 AND holder_token=$2 AND lease_until > clock_timestamp()
                        RETURNING holder_token""", event.identity_key, event.claim_token,
                    )
                    if actor is None:
                        raise _LostClaim
            return True
        except _LostClaim:
            return False

    async def release(self, event: ZaloEvent) -> bool:
        return await self._mutate(
            event, "status='failed',next_attempt_at=clock_timestamp(),locked_at=NULL,claim_token=NULL,lease_until=NULL",
            release=True,
        )

    async def cache_outcome(self, event: ZaloEvent, outcome: dict) -> bool:
        outcome = _decode_outcome(outcome)
        return await self._mutate(event, "assistant_outcome=$4::jsonb", json.dumps(outcome, ensure_ascii=False))

    async def complete(self, event: ZaloEvent, message_id=None) -> bool:
        if message_id is not None and (not isinstance(message_id, str) or not message_id.strip()):
            raise ZaloStorageError("INVALID_PROVIDER_ID")
        return await self._mutate(
            event, "status='done',outbound_message_id=COALESCE($4,outbound_message_id),"
            "completed_at=clock_timestamp(),locked_at=NULL,claim_token=NULL,lease_until=NULL",
            message_id, release=True,
        )

    async def fail(self, event: ZaloEvent, code: str, *, retryable=True) -> bool:
        # Never persist exception text masquerading as a code.
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            code = "STORAGE_UNAVAILABLE"
        return await self._mutate(
            event, "status=CASE WHEN $4 AND attempt_count < $6 "
            "AND received_at > clock_timestamp()-make_interval(hours => $7) THEN 'failed' ELSE 'dead' END,"
            "completed_at=CASE WHEN $4 AND attempt_count < $6 "
            "AND received_at > clock_timestamp()-make_interval(hours => $7) THEN NULL ELSE clock_timestamp() END,"
            "last_error_code=$5,next_attempt_at=clock_timestamp()+make_interval(secs => "
            "LEAST(300,5*power(2,LEAST(6,GREATEST(0,attempt_count-1))))::int),"
            "locked_at=NULL,claim_token=NULL,lease_until=NULL",
            retryable, code, self._settings.zalo_bot_max_attempts,
            self._settings.zalo_bot_retry_max_age_hours, release=True,
        )

    async def reject(self, event: ZaloEvent) -> bool:
        return await self._mutate(
            event, "status='rejected',completed_at=clock_timestamp(),locked_at=NULL,claim_token=NULL,lease_until=NULL",
            release=True,
        )

    async def maintain(self) -> tuple[int, int]:
        if not self._settings.zalo_bot_enabled:
            return 0, 0
        async with _transaction() as conn:
            exhausted = await conn.fetch(
                """WITH expired AS (
                  SELECT id FROM zalo_bot_events e
                  WHERE status IN ('pending','failed','processing')
                    AND (lease_until IS NULL OR lease_until <= clock_timestamp())
                    AND (attempt_count >= $1 OR received_at <= now()-make_interval(hours => $2))
                    AND NOT EXISTS (SELECT 1 FROM zalo_bot_actor_leases a
                      WHERE a.identity_key=e.identity_key AND a.lease_until > clock_timestamp())
                  ORDER BY received_at,id FOR UPDATE SKIP LOCKED LIMIT $3
                ) UPDATE zalo_bot_events e SET status='dead',completed_at=clock_timestamp(),
                  updated_at=clock_timestamp(),locked_at=NULL,claim_token=NULL,lease_until=NULL,
                  last_error_code=CASE WHEN e.assistant_outcome IS NULL AND EXISTS (
                    SELECT 1 FROM zalo_bot_admissions a WHERE a.source_event_key=e.source_event_key
                      AND a.decision='started') THEN 'CORE_OUTCOME_UNCERTAIN'
                    ELSE COALESCE(e.last_error_code,'RETRY_EXHAUSTED') END
                FROM expired x WHERE e.id=x.id RETURNING e.id""",
                self._settings.zalo_bot_max_attempts, self._settings.zalo_bot_retry_max_age_hours,
                self._settings.zalo_bot_batch_size,
            )
            cleaned = await conn.fetch(
                """WITH expired AS (
                  SELECT id FROM zalo_bot_events WHERE status IN ('done','dead','rejected')
                    AND completed_at < now()-make_interval(days => $1)
                    AND claim_token IS NULL AND lease_until IS NULL
                  ORDER BY completed_at,id FOR UPDATE SKIP LOCKED LIMIT $2
                ) DELETE FROM zalo_bot_events e USING expired x WHERE e.id=x.id RETURNING e.id""",
                self._settings.zalo_bot_terminal_retention_days, self._settings.zalo_bot_batch_size,
            )
        return len(exhausted), len(cleaned)


async def _remaining(conn, event_id, identity, token, minimum=0):
    return await conn.fetchval(
        """SELECT extract(epoch FROM (LEAST(a.lease_until,e.lease_until)-clock_timestamp()))
        FROM zalo_bot_actor_leases a JOIN zalo_bot_events e ON e.identity_key=a.identity_key
        WHERE e.id=$1 AND a.identity_key=$2 AND a.holder_token=$3 AND e.claim_token=$3
          AND e.status='processing' AND a.lease_until > clock_timestamp()+make_interval(secs => $4)
          AND e.lease_until > clock_timestamp()+make_interval(secs => $4)""", event_id, identity, token, minimum,
    )


# Catalog contract is shared by startup/ingress and offline backfill. NULL defaults
# are significant: a hidden default must not manufacture claims or identities.
_COLUMNS = {
    "zalo_bot_events": {
        "id": ("bigint", True, "sequence"), "chat_id": ("text", True, None),
        "message_id": ("text", True, None), "event_name": ("text", True, None),
        "text": ("text", True, None), "status": ("text", True, "'pending'::text"),
        "received_at": ("timestamp with time zone", True, "now()"),
        "updated_at": ("timestamp with time zone", True, "now()"),
        "identity_key": ("text", True, None), "source_event_key": ("text", True, None),
        "ingress_key": ("text", True, None), "assistant_outcome": ("jsonb", False, None),
        "outbound_message_id": ("text", False, None), "completed_at": ("timestamp with time zone", False, None),
        "attempt_count": ("integer", True, "0"), "locked_at": ("timestamp with time zone", False, None),
        "lease_until": ("timestamp with time zone", False, None), "claim_token": ("uuid", False, None),
        "next_attempt_at": ("timestamp with time zone", True, "now()"), "last_error_code": ("text", False, None),
    },
    "zalo_bot_ingress_keys": {
        "ingress_key": ("text", True, None), "event_id": ("bigint", True, None),
        "source_event_key": ("text", True, None), "accepted_at": ("timestamp with time zone", True, "now()"),
    },
    "zalo_bot_admissions": {
        "source_event_key": ("text", True, None), "identity_key": ("text", True, None),
        "decision": ("text", True, None), "decided_at": ("timestamp with time zone", True, "now()"),
    },
    "zalo_bot_burst_windows": {
        "identity_key": ("text", True, None), "window_start": ("timestamp with time zone", True, None),
        "admitted_count": ("integer", True, None),
    },
    "zalo_bot_actor_leases": {
        "identity_key": ("text", True, None), "holder_token": ("uuid", True, None),
        "lease_until": ("timestamp with time zone", True, None),
    },
    "zalo_sales_bindings": {
        "opaque_identity": ("text", True, None), "sales_id": ("bigint", True, None),
        "is_active": ("boolean", True, None),
    },
}


async def _validate_schema(conn, *, finalized=True, indexes=True, check_data=True):
    columns = await conn.fetch(
        """SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_expr
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relkind='r' AND c.relname=ANY($1::text[])""",
        list(_COLUMNS),
    )
    actual = {(r["relname"], r["attname"]): r for r in columns}
    for table, specs in _COLUMNS.items():
        for name, (kind, required, default) in specs.items():
            row = actual.get((table, name))
            if not finalized and table == "zalo_bot_events" and name in {"ingress_key", "identity_key", "source_event_key"}:
                required = None
            if (row is None or row["type"] != kind
                    or (required is not None and row["attnotnull"] != required)
                    or (default != "sequence" and row["default_expr"] != default)):
                raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
        if any(r["relname"] == table and r["attname"] not in specs
               and r["attnotnull"] and r["default_expr"] is None for r in columns):
            raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
    sequence = await conn.fetchval("SELECT pg_get_serial_sequence('public.zalo_bot_events','id')")
    if not sequence or not (actual[("zalo_bot_events", "id")]["default_expr"] or "").startswith("nextval("):
        raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
    constraints = await conn.fetch(
        """SELECT c.relname,k.contype,k.convalidated,k.condeferrable,
          pg_get_constraintdef(k.oid) AS definition
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY($1::text[])""", list(_COLUMNS),
    )
    expected = {
        "zalo_bot_events": {"PRIMARY KEY (id)", "UNIQUE (chat_id, message_id)"},
        "zalo_bot_ingress_keys": {"PRIMARY KEY (ingress_key)", "UNIQUE (event_id)", "UNIQUE (source_event_key)"},
        "zalo_bot_admissions": {"PRIMARY KEY (source_event_key)",
            "FOREIGN KEY (source_event_key) REFERENCES zalo_bot_ingress_keys(source_event_key) ON DELETE RESTRICT",
            "CHECK ((decision = ANY (ARRAY['started'::text, 'braked'::text])))"},
        "zalo_bot_burst_windows": {"PRIMARY KEY (identity_key)", "CHECK ((admitted_count >= 0))"},
        "zalo_bot_actor_leases": {"PRIMARY KEY (identity_key)"},
        "zalo_sales_bindings": {"PRIMARY KEY (opaque_identity)",
            "FOREIGN KEY (sales_id) REFERENCES sales(id) ON DELETE RESTRICT"},
    }
    for table, definitions in expected.items():
        found = {r["definition"].replace("public.", "") for r in constraints
                 if r["relname"] == table and r["convalidated"] and not r["condeferrable"]}
        if not definitions <= found:
            # Baseline ingress migration also permits a standalone raw-pair unique index.
            if table != "zalo_bot_events" or definitions - found != {"UNIQUE (chat_id, message_id)"}:
                raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
        if any(r["relname"] == table and r["contype"] == 'f'
               and r["definition"].replace("public.", "") not in definitions for r in constraints):
            raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
    index_rows = await conn.fetch(
        """SELECT c.relname AS table_name,ic.relname AS index_name,i.indisvalid,i.indisready,
          i.indisunique,i.indimmediate,i.indpred IS NULL AS unconditional,
          i.indexprs IS NULL AS plain,i.indnkeyatts,
          ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(num,ord)
            JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
            WHERE k.ord<=i.indnkeyatts ORDER BY k.ord) AS columns,
          pg_get_expr(i.indpred,i.indrelid) AS predicate
        FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid
        JOIN pg_class ic ON ic.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=ANY($1::text[])""", list(_COLUMNS),
    )
    def valid(r):
        return r["indisvalid"] and r["indisready"] and r["indimmediate"] and r["plain"]
    if not any(r["table_name"] == "zalo_bot_events" and valid(r) and r["indisunique"]
               and r["unconditional"] and sorted(r["columns"]) == ["chat_id", "message_id"] for r in index_rows):
        raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
    for r in index_rows:
        if not valid(r):
            raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
    if indexes:
        wanted = {
            "zalo_bot_events_ingress_key_uidx": (["ingress_key"], True, None),
            "zalo_bot_events_source_key_uidx": (["source_event_key"], True, None),
            "zalo_bot_events_due_idx": (["next_attempt_at", "received_at", "id"], False,
                "(status = ANY (ARRAY['pending'::text, 'failed'::text, 'processing'::text]))"),
            "zalo_bot_events_terminal_idx": (["completed_at", "id"], False,
                "(status = ANY (ARRAY['done'::text, 'dead'::text, 'rejected'::text]))"),
        }
        for name, (keys, unique, predicate) in wanted.items():
            matches = [r for r in index_rows if r["index_name"] == name]
            if (len(matches) != 1 or matches[0]["table_name"] != "zalo_bot_events"
                    or list(matches[0]["columns"]) != keys or matches[0]["indisunique"] != unique
                    or matches[0]["predicate"] != predicate):
                raise ZaloStorageError("SCHEMA_INCOMPATIBLE")
    if check_data:
        bad = await conn.fetchval(
            """SELECT EXISTS (SELECT 1 FROM zalo_bot_events e
            LEFT JOIN zalo_bot_ingress_keys k ON k.ingress_key=e.ingress_key
            LEFT JOIN zalo_bot_admissions a ON a.source_event_key=e.source_event_key
            WHERE e.ingress_key IS NULL OR e.identity_key IS NULL OR e.source_event_key IS NULL
              OR k.event_id IS DISTINCT FROM e.id OR k.source_event_key IS DISTINCT FROM e.source_event_key
              OR e.source_event_key <> 'zalo-bot:event:v1:' || e.id::text
              OR e.status NOT IN ('pending','processing','failed','done','dead','rejected')
              OR e.attempt_count < 0 OR (a.identity_key IS NOT NULL AND a.identity_key<>e.identity_key)
              OR ((e.assistant_outcome IS NOT NULL OR e.outbound_message_id IS NOT NULL) AND a.source_event_key IS NULL)
              OR (e.status IN ('done','dead','rejected') AND (e.completed_at IS NULL
                OR e.claim_token IS NOT NULL OR e.lease_until IS NOT NULL OR e.locked_at IS NOT NULL)))
              OR EXISTS (SELECT 1 FROM zalo_bot_events GROUP BY ingress_key HAVING count(*)>1)
              OR EXISTS (SELECT 1 FROM zalo_bot_events GROUP BY source_event_key HAVING count(*)>1)"""
        )
        if bad:
            raise ZaloStorageError("BACKFILL_REQUIRED")
