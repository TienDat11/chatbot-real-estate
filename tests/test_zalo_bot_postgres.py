"""Opt-in, destructive ONLY inside an explicitly approved empty disposable DB.

All tests carry existing ``e2e`` marker; explicit file selection fails (never
skips) without prerequisites. No provider/RAG calls. Direct PG is NOT pooler proof.

Required: ZALO_TEST_DATABASE_URL, ZALO_TEST_ALLOW_SCHEMA_CHANGES=1 and
ZALO_TEST_APPROVAL_FILE pointing to separately reviewed JSON, for example::

    {
      "approved_by": "operator/change-reference", "disposable": true,
      "direct": {"host": "127.0.0.1", "port": 5432,
                 "database": "zb03_disposable", "user": "zb03_test"},
      "known_production_targets": [
        {"host": "production.example", "port": 5432,
         "database": "production", "user": "production"}
      ],
      "pooler": {"host": "pooler.example", "port": 6543,
                 "database": "zb03_disposable", "user": "zb03_test"},
      "pooler_mode": "transaction",
      "pooler_mode_evidence": "operator-reviewed configuration/change reference",
      "pooler_database_target": {"host": "127.0.0.1", "port": 5432,
                                 "database": "zb03_disposable", "user": "zb03_test"}
    }

Pooler test additionally requires ZALO_TEST_POOLER_DATABASE_URL. Approval file
contains no passwords. Same-DB sentinel verifies mapping, not transaction mode;
mode requires independent operator evidence. Pools close on their owning loop.
The fixture refuses existing public objects; it never drops DB/public schema.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import multiprocessing
import os
from pathlib import Path
import re
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from pydantic import SecretStr

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[1]
SECRET = "zb03-disposable-identity-secret-not-a-provider-credential"
MIGRATIONS = ROOT / "db/migrations"
OWNED_TABLES = (
    "zalo_bot_admissions", "zalo_bot_actor_leases", "zalo_bot_burst_windows",
    "zalo_bot_ingress_keys", "zalo_bot_events", "zalo_sales_bindings",
    "chat_messages", "chat_sessions", "leads", "sales", "quota_reservations",
    "quota_grant_audit", "anon_quota", "ip_rate_limit",
)
_CURRENT_POOL: ContextVar[asyncpg.Pool | None] = ContextVar("zb03_test_pool", default=None)


def _target(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname
            or not parsed.username or not parsed.path.strip("/")
            or parsed.query or parsed.fragment):
        raise ValueError("ZALO_TEST_INVALID_DATABASE_URL")
    return {"host": parsed.hostname.lower(), "port": parsed.port or 5432,
            "database": unquote(parsed.path[1:]), "user": unquote(parsed.username)}


def _approved_url(kind="direct"):
    key = "ZALO_TEST_DATABASE_URL" if kind == "direct" else "ZALO_TEST_POOLER_DATABASE_URL"
    url = os.environ.get(key, "")
    if not url or os.environ.get("ZALO_TEST_ALLOW_SCHEMA_CHANGES") != "1":
        pytest.fail(f"{key} and ZALO_TEST_ALLOW_SCHEMA_CHANGES=1 required", pytrace=False)
    try:
        approval = json.loads(Path(os.environ["ZALO_TEST_APPROVAL_FILE"]).read_text(encoding="utf-8"))
        target = _target(url)
        if (approval.get("disposable") is not True or not approval.get("approved_by")
                or target != approval[kind] or not approval["known_production_targets"]):
            raise ValueError
        # Refuse known production regardless of role/user alias.
        for production in approval["known_production_targets"]:
            if all(target[name] == production[name] for name in ("host", "port", "database")):
                raise ValueError
        if target["database"].lower() in {"postgres", "template0", "template1", "ragre", "production", "prod"}:
            raise ValueError
        ambient_host = os.environ.get("POSTGRES_HOST", "").lower()
        ambient_db = os.environ.get("POSTGRES_DATABASE", "")
        if ambient_host and target["host"] == ambient_host and target["database"] == ambient_db:
            raise ValueError
        if kind == "pooler" and (
            approval.get("pooler_mode") != "transaction"
            or not approval.get("pooler_mode_evidence")
            or approval.get("pooler_database_target") != approval["direct"]
            or target == approval["direct"]
        ):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError, OSError):
        pytest.fail("ZALO_TEST_EXACT_DISPOSABLE_APPROVAL_REQUIRED", pytrace=False)
    return url, approval


def _settings(url):
    target = _target(url)
    return SimpleNamespace(
        postgres_host=target["host"], postgres_port=target["port"],
        postgres_database=target["database"], postgres_user=target["user"],
        postgres_password=unquote(urlsplit(url).password or ""),
        zalo_bot_identity_secret=SecretStr(SECRET), zalo_bot_enabled=True,
        zalo_bot_lease_seconds=120, zalo_bot_core_timeout_seconds=90,
        zalo_bot_lease_margin_seconds=15, zalo_bot_poll_seconds=1,
        zalo_bot_batch_size=100, zalo_bot_max_attempts=8,
        zalo_bot_retry_max_age_hours=24, zalo_bot_terminal_retention_days=7,
        zalo_bot_burst_window_seconds=300, zalo_bot_burst_max_events=3,
    )


@asynccontextmanager
async def _real_pool(url):
    """Keep actual build_dsn/get_lead_pool/asyncpg/_pooler_safe_kwargs in path."""
    from api.infrastructure.config import config
    from api.infrastructure.adapters import postgres_leads

    cfg = _settings(url)
    old_settings = config.get_settings
    old_pool, old_loop = postgres_leads._lead_pool, postgres_leads._lead_pool_loop
    old_locks = postgres_leads._pool_locks
    pool = None
    # Preserve original cached Settings function, including its cache; never
    # populate it from ambient .env. api.get_cfg resolves this module dynamically.
    config.get_settings = lambda: cfg
    postgres_leads._lead_pool = postgres_leads._lead_pool_loop = None
    postgres_leads._pool_locks = {}
    try:
        # Production DSN builder is intentionally retained. Fail before opening
        # any socket if its serialization differs from approved target.
        if _target(postgres_leads.build_dsn()) != _target(url):
            pytest.fail("ZALO_TEST_CONNECTION_TARGET_MISMATCH", pytrace=False)
        pool = await postgres_leads.get_lead_pool()
        yield pool, cfg
    finally:
        try:
            if pool is not None:
                await asyncio.wait_for(pool.close(), 10)
        finally:
            postgres_leads._lead_pool, postgres_leads._lead_pool_loop = old_pool, old_loop
            postgres_leads._pool_locks = old_locks
            config.get_settings = old_settings




async def _on(pool, operation, *args, **kwargs):
    token = _CURRENT_POOL.set(pool)
    try:
        return await operation(*args, **kwargs)
    finally:
        _CURRENT_POOL.reset(token)


async def _migration(pool, name):
    sql = (MIGRATIONS / name).read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        if name.endswith("-indexes.sql"):
            # Preserve dollar-quoted preflight as one command; each subsequent
            # CONCURRENTLY statement remains its own top-level command.
            preflight, separator, indexes = sql.partition("$preflight$;")
            assert separator, "index migration preflight delimiter missing"
            await conn.execute(preflight + separator)
            for statement in indexes.split(";"):
                if statement.strip():
                    await conn.execute(statement)
        else:
            await conn.execute(sql)


async def _baseline(pool):
    schema = (ROOT / "db/lead_schema.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        for table in ("sales", "leads"):
            ddl = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?^\);", schema, re.S | re.M)
            assert ddl is not None
            await conn.execute(ddl[0])
    await _migration(pool, "2026-09-17-zalo-bot-events.sql")
    await _migration(pool, "2026-09-17-zalo-bot-roundtrip.sql")
    await _migration(pool, "2026-08-24-anon-quota-and-ip-rate-limit.sql")
    await _migration(pool, "2026-08-25-chat-session-history.sql")


async def _cutover(pool):
    from scripts.backfill_zalo_bot_hardening import backfill
    await _migration(pool, "2026-09-17-zalo-bot-hardening.sql")
    await backfill(pool, SECRET)
    await _migration(pool, "2026-09-17-zalo-bot-hardening-indexes.sql")
    await _migration(pool, "2026-09-17-zalo-bot-hardening-finalize.sql")


@dataclass
class Database:
    pool: asyncpg.Pool
    other: asyncpg.Pool
    settings: object
    store: object
    admission: object


@pytest_asyncio.fixture
async def database(monkeypatch):
    url, _ = _approved_url()
    # Deferred imports keep broad offline deselection free of new source imports.
    from api.infrastructure.adapters import postgres_zalo_events as events
    from api.infrastructure.adapters import postgres_zalo_bindings as bindings
    from api.infrastructure.adapters import postgres_zalo_admission as admissions
    from scripts import backfill_zalo_bot_hardening as backfill_script
    # Offline backfill requires disabled writers; live adapter settings below
    # remain enabled solely to exercise disposable transport operations.
    monkeypatch.setattr(backfill_script, "get_settings", lambda: SimpleNamespace(zalo_bot_enabled=False))

    async with _real_pool(url) as (pool, settings):
        async with pool.acquire() as conn:
            existing = await conn.fetchval("""SELECT count(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')
                AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid
                    AND d.classid='pg_class'::regclass AND d.deptype='e')""")
            existing += await conn.fetchval("""SELECT count(*) FROM pg_proc p
                JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'
                AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=p.oid
                    AND d.classid='pg_proc'::regclass AND d.deptype='e')""")
            if existing:
                pytest.fail("ZALO_TEST_REQUIRES_EMPTY_DISPOSABLE_PUBLIC_SCHEMA", pytrace=False)
        # A second independently created real pool, not a fake acquire wrapper.
        async with _real_pool(url) as (other, _):
            async def selected_pool():
                # Async-fixture task ContextVars need not propagate to test task.
                # Explicit concurrency overrides win; otherwise use fixture pool.
                return _CURRENT_POOL.get() or pool

            for module in (events, bindings, admissions):
                monkeypatch.setattr(module, "get_lead_pool", selected_pool)
            monkeypatch.setattr(events, "get_settings", lambda: settings)
            try:
                await _baseline(pool)
                yield Database(pool, other, settings,
                               events.PostgresZaloEventStore(settings=settings),
                               admissions.PostgresZaloAdmission(settings=settings))
            finally:
                async with pool.acquire() as conn:
                    # Empty-schema preflight makes these exclusively test-owned.
                    # No CASCADE: unexpected dependencies must fail cleanup loudly.
                    for table in OWNED_TABLES:
                        await conn.execute(f'DROP TABLE IF EXISTS public."{table}"')
                    await conn.execute("DROP FUNCTION IF EXISTS public.zb03_test_fail_event()")
                    await conn.execute("DROP FUNCTION IF EXISTS public.zb03_test_fail_admission()")


async def _insert(chat="actor", message=None):
    from api.infrastructure.adapters.postgres_zalo_events import insert_event
    return await insert_event(chat_id=chat, message_id=message or str(uuid4()),
                              event_name="message.text.received", text="disposable test text")


async def _claim(db, chat="actor"):
    event_id = await _insert(chat)
    event = await db.store.claim_event(event_id)
    assert event is not None
    return event


async def _admit(db, event):
    return await db.admission.admit(event.identity_key, event.source_event_key,
                                    event_id=event.id, claim_token=event.claim_token)


async def _expire(pool, event):
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE zalo_bot_actor_leases SET lease_until=clock_timestamp()-interval '1 second' WHERE identity_key=$1", event.identity_key)
            await conn.execute("UPDATE zalo_bot_events SET lease_until=clock_timestamp()-interval '1 second', next_attempt_at=clock_timestamp()-interval '1 second' WHERE id=$1", event.id)


@pytest.mark.parametrize("legacy", [False, True], ids=["empty", "pre-zb03"])
async def test_actual_migrations_backfill_repeat_and_final_constraints(database, legacy):
    from api.application.services.zalo_bot_identity import ingress_key, opaque_identity
    from scripts.backfill_zalo_bot_hardening import backfill
    db = database
    old = []
    if legacy:
        async with db.pool.acquire() as conn:
            for status, entered, cached in (("pending", False, False), ("processing", True, False), ("processing", False, True), ("rejected", False, False), ("done", False, True)):
                row = await conn.fetchrow("""INSERT INTO zalo_bot_events(chat_id,message_id,event_name,text,status,assistant_outcome)
                    VALUES($1,$2,'message.text.received','legacy text',$3,$4::jsonb) RETURNING *""",
                    "legacy-" + status + ("-cached" if cached else ""), status, status,
                    json.dumps({"answer": "persisted", "resolution_status": "answered", "project_key": None, "meta": {}}) if cached else None)
                if entered:
                    await conn.execute("UPDATE zalo_bot_events SET source_event_key=$2 WHERE id=$1", row["id"], f'zalo-bot:event:v1:{row["id"]}')
                old.append((dict(row), entered or cached))
    await _migration(db.pool, "2026-09-17-zalo-bot-hardening.sql")
    processing = [original for original, _ in old if original["status"] == "processing"]
    due = await db.store.due_candidates()
    for original in processing:
        async with db.pool.acquire() as conn:
            assert await conn.fetchval("SELECT lease_until FROM zalo_bot_events WHERE id=$1", original["id"]) is None
        assert original["id"] not in due
        assert await db.store.claim_event(original["id"]) is None
    await _cutover(db.pool)
    await db.store.validate_schema()
    async with db.pool.acquire() as conn:
        before = await conn.fetch("SELECT * FROM zalo_bot_events ORDER BY id")
        receipts = await conn.fetch("SELECT * FROM zalo_bot_admissions ORDER BY source_event_key")
    await _migration(db.pool, "2026-09-17-zalo-bot-events.sql")
    await _migration(db.pool, "2026-09-17-zalo-bot-roundtrip.sql")
    await _cutover(db.pool)
    assert await backfill(db.pool, SECRET) == 0
    async with db.pool.acquire() as conn:
        assert await conn.fetch("SELECT * FROM zalo_bot_events ORDER BY id") == before
        assert await conn.fetch("SELECT * FROM zalo_bot_admissions ORDER BY source_event_key") == receipts
        for original, possible_core in old:
            row = await conn.fetchrow("SELECT * FROM zalo_bot_events WHERE id=$1", original["id"])
            assert row["ingress_key"] == ingress_key(original["chat_id"], original["message_id"], SECRET)
            assert row["identity_key"] == opaque_identity(original["chat_id"], SECRET)
            assert row["source_event_key"] == f'zalo-bot:event:v1:{original["id"]}'
            assert row["assistant_outcome"] == original["assistant_outcome"]
            assert (await conn.fetchval("SELECT count(*) FROM zalo_bot_admissions WHERE source_event_key=$1", row["source_event_key"])) == int(possible_core)
            if original["status"] == "processing":
                assert row["status"] == "processing"
                assert row["lease_until"] == original["received_at"]
            if original["status"] in {"done", "rejected"}:
                assert row["completed_at"] == original["updated_at"]
        indexes = await conn.fetch("""SELECT c.relname,i.indisvalid,i.indisready FROM pg_index i
            JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname=ANY($1::text[])""",
            ["zalo_bot_events_ingress_key_uidx", "zalo_bot_events_source_key_uidx", "zalo_bot_events_due_idx", "zalo_bot_events_terminal_idx"])
        assert len(indexes) == 4 and all(row["indisvalid"] and row["indisready"] for row in indexes)
        for column in ("ingress_key", "identity_key", "source_event_key"):
            assert await conn.fetchval("SELECT attnotnull FROM pg_attribute WHERE attrelid='public.zalo_bot_events'::regclass AND attname=$1", column)
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute("INSERT INTO zalo_bot_events(chat_id,message_id,event_name,text) VALUES('old-writer','m','text','x')")
    await db.store.validate_schema()
    due = await db.store.due_candidates()
    for original in processing:
        assert original["id"] in due
        async with db.pool.acquire() as conn:
            attempts_before_claim = await conn.fetchval("SELECT attempt_count FROM zalo_bot_events WHERE id=$1", original["id"])
        claimed = await db.store.claim_event(original["id"])
        assert claimed is not None and claimed.id == original["id"]
        assert claimed.status == "processing"
        assert claimed.attempt_count == attempts_before_claim + 1
        expected_cache = original["assistant_outcome"]
        if isinstance(expected_cache, str):
            expected_cache = json.loads(expected_cache)
        assert claimed.assistant_outcome == expected_cache


async def test_processing_null_lease_blocks_cutover_but_not_new_ingress(database):
    from api.infrastructure.adapters.postgres_zalo_events import ZaloStorageError
    from scripts.backfill_zalo_bot_hardening import backfill
    db = database
    await _cutover(db.pool)
    stranded_id = await _insert("stranded")
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE zalo_bot_events SET status='processing',lease_until=received_at WHERE id=$1", stranded_id)
    await db.store.validate_schema()
    # Only the lease changes: mappings, tombstone and schema already pass.
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE zalo_bot_events SET lease_until=NULL WHERE id=$1", stranded_id)
    with pytest.raises(ZaloStorageError) as exc:
        await db.store.validate_schema()
    assert exc.value.code == "BACKFILL_REQUIRED"
    assert stranded_id not in await db.store.due_candidates()
    assert await db.store.claim_event(stranded_id) is None
    new_id = await _insert("new-during-cutover")
    assert new_id is not None
    async with db.pool.acquire() as conn:
        new_row = await conn.fetchrow("SELECT status,lease_until FROM zalo_bot_events WHERE id=$1", new_id)
        assert new_row["status"] == "pending" and new_row["lease_until"] is None
    new_claim = await db.store.claim_event(new_id)
    assert new_claim is not None and new_claim.id == new_id
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT lease_until > now() FROM zalo_bot_events WHERE id=$1", new_id)
    assert await backfill(db.pool, SECRET) == 1
    await db.store.validate_schema()
    assert stranded_id in await db.store.due_candidates()
    recovered = await db.store.claim_event(stranded_id)
    assert recovered is not None and recovered.id == stranded_id


async def test_backfill_conflict_rolls_back_batch_and_finalize_refuses_nulls(database):
    from scripts.backfill_zalo_bot_hardening import backfill
    from api.infrastructure.adapters.postgres_zalo_events import ZaloStorageError
    db = database
    await _migration(db.pool, "2026-09-17-zalo-bot-hardening.sql")
    async with db.pool.acquire() as conn:
        await conn.execute("""INSERT INTO zalo_bot_events(chat_id,message_id,event_name,text,source_event_key)
            VALUES('ok','one','text','x',NULL),('bad','two','text','x','conflicting-source')""")
    with pytest.raises(ZaloStorageError) as exc:
        await backfill(db.pool, SECRET)
    assert exc.value.code == "BACKFILL_CONFLICT"
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_ingress_keys") == 0
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_events WHERE ingress_key IS NOT NULL") == 0
        with pytest.raises(asyncpg.RaiseError, match="ZALO_BACKFILL_REQUIRED"):
            await conn.execute((MIGRATIONS / "2026-09-17-zalo-bot-hardening-finalize.sql").read_text(encoding="utf-8"))
        await conn.execute("ROLLBACK")
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_events") == 2


async def test_index_preflight_rejects_incompatible_existing_index(database):
    db = database
    await _cutover(db.pool)
    async with db.pool.acquire() as conn:
        await conn.execute("DROP INDEX zalo_bot_events_due_idx")
        await conn.execute("CREATE INDEX zalo_bot_events_due_idx ON zalo_bot_events (id)")
    with pytest.raises(asyncpg.RaiseError, match="ZALO_INDEX_INCOMPATIBLE"):
        await _migration(db.pool, "2026-09-17-zalo-bot-hardening-indexes.sql")


async def test_binding_foreign_keys_restrict_conflicts_and_revocation(database):
    from api.application.services.zalo_bot_identity import opaque_identity
    from api.infrastructure.adapters import postgres_zalo_bindings as bindings
    db = database
    await _cutover(db.pool)
    identity = opaque_identity("bound", SECRET)
    async with db.pool.acquire() as conn:
        sales = await conn.fetchval("INSERT INTO sales(full_name) VALUES('test sales') RETURNING id")
        other = await conn.fetchval("INSERT INTO sales(full_name) VALUES('other sales') RETURNING id")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute("INSERT INTO zalo_sales_bindings VALUES($1,9223372036854775807,true)", identity)
    assert await bindings.provision_binding("bound", sales, SECRET, ownership_verified=True)
    assert await bindings.provision_binding("bound", sales, SECRET, ownership_verified=True)
    assert not await bindings.provision_binding("bound", other, SECRET, ownership_verified=True)
    assert await db.store.authorize(identity) == sales
    async with db.pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute("DELETE FROM sales WHERE id=$1", sales)
    assert await bindings.revoke_binding(identity)
    assert await bindings.provision_binding("bound", sales, SECRET, ownership_verified=True)
    assert await db.store.authorize(identity) is None
    assert await bindings.reenable_binding(identity, sales, approved=True)
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE sales SET is_active=false WHERE id=$1", sales)
    assert await db.store.authorize(identity) is None


async def test_concurrent_ingress_rollback_and_purge_redelivery(database):
    from api.infrastructure.adapters.postgres_zalo_events import ZaloStorageError
    db = database
    await _cutover(db.pool)
    results = await asyncio.gather(_on(db.pool, _insert, "same", "message"), _on(db.other, _insert, "same", "message"))
    assert sum(value is not None for value in results) == 1
    event_id = next(value for value in results if value is not None)
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_events") == 1
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_ingress_keys") == 1
        await conn.execute("""CREATE FUNCTION zb03_test_fail_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'ZB03_TEST_ROLLBACK'; END; $$;
            CREATE TRIGGER zb03_test_fail BEFORE INSERT ON zalo_bot_events
            FOR EACH ROW EXECUTE FUNCTION zb03_test_fail_event()""")
    with pytest.raises(ZaloStorageError):
        await _insert("rollback", "message")
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_ingress_keys") == 1
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_events") == 1
        await conn.execute("DROP TRIGGER zb03_test_fail ON zalo_bot_events")
    assert await _insert("rollback", "message") is not None
    assert await _insert("a:b", "c") != await _insert("a", "b:c")
    event = await db.store.claim_event(event_id)
    assert await db.store.complete(event, message_id="provider-id")
    async with db.pool.acquire() as conn:
        source = await conn.fetchval("SELECT source_event_key FROM zalo_bot_ingress_keys WHERE event_id=$1", event_id)
        await conn.execute("UPDATE zalo_bot_events SET completed_at=now()-interval '8 days' WHERE id=$1", event_id)
    assert (await db.store.maintain())[1] == 1
    assert await _on(db.other, _insert, "same", "message") is None
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_events WHERE id=$1", event_id) == 0
        assert await conn.fetchval("SELECT source_event_key FROM zalo_bot_ingress_keys WHERE event_id=$1", event_id) == source


async def test_dual_lease_stale_fencing_renew_rollback_and_recovery(database):
    from api.application.ports.zalo_bot import ZaloAdmissionDecision as Decision
    db = database
    await _cutover(db.pool)
    event_id = await _insert()
    claims = await asyncio.gather(_on(db.pool, db.store.claim_event, event_id), _on(db.other, db.store.claim_event, event_id))
    assert sum(event is not None for event in claims) == 1
    old = next(event for event in claims if event is not None)
    assert old.attempt_count == 1
    assert await _admit(db, old) == Decision.STARTED
    async with db.pool.acquire() as conn:
        actor_expiry = await conn.fetchval("SELECT lease_until FROM zalo_bot_actor_leases WHERE identity_key=$1", old.identity_key)
        # Event half fails after actor UPDATE: entire renewal must roll back.
        await conn.execute("UPDATE zalo_bot_events SET claim_token=$2 WHERE id=$1", old.id, uuid4())
    assert await db.store.renew_lease(old) is None
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT lease_until FROM zalo_bot_actor_leases WHERE identity_key=$1", old.identity_key) == actor_expiry
        await conn.execute("UPDATE zalo_bot_events SET claim_token=$2 WHERE id=$1", old.id, UUID(str(old.claim_token)))
    assert await db.store.renew_lease(old) is not None
    await _expire(db.pool, old)
    assert await db.store.renew_lease(old) is None
    new = await _on(db.other, db.store.claim_event, old.id)
    assert new is not None and new.claim_token != old.claim_token and new.attempt_count == 2
    assert await _admit(db, new) == Decision.ALREADY_STARTED
    assert await db.store.check_ownership(old) is None
    assert await db.store.renew_lease(old) is None
    assert not await db.store.release(old)
    assert not await db.store.cache_outcome(old, {"answer": "stale", "resolution_status": "answered", "project_key": None, "meta": {}})
    assert not await db.store.complete(old, message_id="stale-provider")
    assert not await db.store.fail(old, "STALE")
    assert not await db.store.reject(old)
    assert await db.store.check_ownership(new) is not None
    assert await db.store.complete(new, message_id="fresh-provider")


async def test_threshold_concurrency_duplicate_and_window_rollover(database):
    from api.application.ports.zalo_bot import ZaloAdmissionDecision as Decision
    db = database
    await _cutover(db.pool)
    # Avoid nondeterminism at a real DB window boundary, without mocking DB time.
    async with db.pool.acquire() as conn:
        remaining = await conn.fetchval("SELECT 300-mod(extract(epoch FROM clock_timestamp()),300)")
    if remaining < 30:
        await asyncio.sleep(float(remaining) + 0.1)
    ids = [await _insert("burst") for _ in range(7)]

    async def compete(pool, event_id):
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            event = await _on(pool, db.store.claim_event, event_id)
            if event is not None:
                decision = await _on(pool, _admit, db, event)
                assert await _on(pool, db.store.complete, event)
                return decision
            await asyncio.sleep(0.02)
        pytest.fail("actor failed to progress after independent pool contention")

    decisions = await asyncio.gather(*(compete(db.pool if i % 2 else db.other, event_id) for i, event_id in enumerate(ids)))
    assert decisions.count(Decision.STARTED) == 3
    assert decisions.count(Decision.BRAKED) == 4
    event = await _claim(db, "separate")
    results = await asyncio.gather(_on(db.pool, _admit, db, event), _on(db.other, _admit, db, event))
    assert results.count(Decision.STARTED) == 1
    assert results.count(Decision.ALREADY_STARTED) == 1
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT admitted_count FROM zalo_bot_burst_windows WHERE identity_key=$1", event.identity_key) == 1
        await conn.execute("UPDATE zalo_bot_burst_windows SET window_start=window_start-interval '1 day' WHERE identity_key=$1", event.identity_key)
    assert await _admit(db, event) == Decision.ALREADY_STARTED
    assert await db.store.complete(event)
    next_event = await _claim(db, "separate")
    assert await _admit(db, next_event) == Decision.STARTED
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT admitted_count FROM zalo_bot_burst_windows WHERE identity_key=$1", event.identity_key) == 1


@pytest.mark.parametrize("braked", [False, True], ids=["started", "braked"])
async def test_existing_receipt_bypasses_locked_counter(database, braked):
    from api.application.ports.zalo_bot import ZaloAdmissionDecision as Decision
    db = database
    await _cutover(db.pool)
    db.settings.zalo_bot_burst_max_events = 1
    if braked:
        first = await _claim(db)
        assert await _admit(db, first) == Decision.STARTED
        assert await db.store.complete(first)
    event = await _claim(db)
    assert await _admit(db, event) == (Decision.BRAKED if braked else Decision.STARTED)
    async with db.other.acquire() as blocker:
        async with blocker.transaction():
            await blocker.execute("SELECT 1 FROM zalo_bot_burst_windows WHERE identity_key=$1 FOR UPDATE", event.identity_key)
            result = await asyncio.wait_for(_admit(db, event), 1)
            assert result == (Decision.ALREADY_BRAKED if braked else Decision.ALREADY_STARTED)
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE zalo_bot_burst_windows SET window_start=window_start-interval '1 day' WHERE identity_key=$1", event.identity_key)
    await _expire(db.pool, event)
    recovered = await _on(db.other, db.store.claim_event, event.id)
    assert recovered is not None
    assert await _on(db.other, _admit, db, recovered) == (Decision.ALREADY_BRAKED if braked else Decision.ALREADY_STARTED)
    event = recovered
    assert await db.store.complete(event)
    new = await _claim(db)
    async with db.other.acquire() as blocker:
        async with blocker.transaction():
            await blocker.execute("SELECT 1 FROM zalo_bot_burst_windows WHERE identity_key=$1 FOR UPDATE", new.identity_key)
            task = asyncio.create_task(_admit(db, new))
            try:
                done, _ = await asyncio.wait({task}, timeout=0.2)
                if done:
                    assert task.exception() is not None or task.result() != Decision.STARTED
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM zalo_bot_admissions WHERE source_event_key=$1", new.source_event_key) == 0


@pytest.mark.parametrize("sqlstate", ["P0001", "23505"], ids=["rollback", "pk-conflict"])
async def test_admission_failure_rolls_back_receipt_and_counter(database, sqlstate):
    from api.application.ports.zalo_bot import ZaloAdmissionDecision as Decision, ZaloAdmissionUnavailable
    db = database
    await _cutover(db.pool)
    first = await _claim(db)
    assert await _admit(db, first) == Decision.STARTED
    assert await db.store.complete(first)
    event = await _claim(db)
    before = await _snapshot(db.pool, ("zalo_bot_admissions", "zalo_bot_burst_windows"))
    async with db.pool.acquire() as conn:
        # Both receipt and counter writes have occurred; even a source-PK-shaped
        # error must roll back both and duplicate-only retry cannot grant STARTED.
        await conn.execute(f"""CREATE FUNCTION zb03_test_fail_admission() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'ZB03_TEST_ROLLBACK' USING ERRCODE='{sqlstate}'; END; $$;
            CREATE TRIGGER zb03_test_fail AFTER UPDATE ON zalo_bot_burst_windows
            FOR EACH ROW EXECUTE FUNCTION zb03_test_fail_admission()""")
    with pytest.raises(ZaloAdmissionUnavailable):
        await _admit(db, event)
    assert await _snapshot(db.pool, tuple(before)) == before
    async with db.pool.acquire() as conn:
        await conn.execute("DROP TRIGGER zb03_test_fail ON zalo_bot_burst_windows")
    assert await _admit(db, event) == Decision.STARTED
    assert await _admit(db, event) == Decision.ALREADY_STARTED
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT admitted_count FROM zalo_bot_burst_windows WHERE identity_key=$1", event.identity_key) == 2


async def _snapshot(pool, tables):
    async with pool.acquire() as conn:
        return {table: await conn.fetchval(f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb) FROM {table} t") for table in tables}


async def test_retention_preserves_receipts_bindings_history_customer_credit_and_leads(database):
    from api.application.services.zalo_bot_identity import opaque_identity
    from api.infrastructure.adapters.postgres_zalo_events import PostgresZaloEventStore
    db = database
    await _cutover(db.pool)
    async with db.pool.acquire() as conn:
        sales = await conn.fetchval("INSERT INTO sales(full_name) VALUES('retention sales') RETURNING id")
        await conn.execute("INSERT INTO zalo_sales_bindings VALUES($1,$2,false)", opaque_identity("retained", SECRET), sales)
        await conn.execute("INSERT INTO leads(phone,consent) VALUES('test-only',true)")
        await conn.execute("INSERT INTO anon_quota(identity_key,project_key,used_turns,bonus_turns,granted_turns,bonus_granted) VALUES('customer','project',999,4,11,true)")
        await conn.execute("INSERT INTO quota_grant_audit(request_id,identity_key,project_key,granted_turns) VALUES('staff-credit','customer','project',11)")
        await conn.execute("INSERT INTO quota_reservations VALUES($1,'customer','project','granted',now())", uuid4())
        await conn.execute("INSERT INTO chat_sessions(session_id,identity_key,project_key) VALUES('global-history','customer','global')")
        await conn.execute("INSERT INTO chat_messages(session_id,role,content) VALUES('global-history','assistant','preserved global answer')")
    protected = ("sales", "zalo_sales_bindings", "leads", "anon_quota", "quota_grant_audit", "quota_reservations", "chat_sessions", "chat_messages")
    before = await _snapshot(db.pool, protected)
    purged = []
    for terminal in ("done", "dead", "rejected"):
        event = await _claim(db, terminal)
        await _admit(db, event)
        if terminal == "done":
            assert await db.store.complete(event, message_id="provider")
        elif terminal == "dead":
            assert await db.store.fail(event, "PERMANENT", retryable=False)
        else:
            assert await db.store.reject(event)
        purged.append(event.id)
    fresh = await _claim(db, "fresh")
    assert await db.store.complete(fresh)
    active = await _claim(db, "active")
    retryable = await _claim(db, "retryable")
    assert await db.store.fail(retryable, "TRANSIENT")
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE zalo_bot_events SET completed_at=now()-interval '8 days' WHERE id=ANY($1::bigint[])", purged)
        # Bad old completion timestamps alone do not authorize active-row purge.
        await conn.execute("UPDATE zalo_bot_events SET completed_at=now()-interval '8 days' WHERE id=ANY($1::bigint[])", [active.id, retryable.id])
    durable = await _snapshot(db.pool, ("zalo_bot_ingress_keys", "zalo_bot_admissions", "zalo_bot_burst_windows", "zalo_bot_actor_leases"))
    restarted = PostgresZaloEventStore(settings=db.settings)
    assert (await _on(db.other, restarted.maintain))[1] == 3
    assert await _snapshot(db.pool, protected) == before
    assert await _snapshot(db.pool, tuple(durable)) == durable
    async with db.pool.acquire() as conn:
        remaining = await conn.fetch("SELECT id FROM zalo_bot_events")
        assert {row["id"] for row in remaining} == {fresh.id, active.id, retryable.id}
        for event_id in purged:
            source = f"zalo-bot:event:v1:{event_id}"
            assert await conn.fetchval("SELECT count(*) FROM zalo_bot_admissions WHERE source_event_key=$1", source) == 1
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute("DELETE FROM zalo_bot_ingress_keys WHERE source_event_key=$1", source)


async def test_exhaustion_retains_spent_receipt_before_and_after_purge(database):
    from api.application.ports.zalo_bot import ZaloAdmissionDecision as Decision
    db = database
    await _cutover(db.pool)
    event_id = await _insert("exhausted", "stable-message")
    event = await db.store.claim_event(event_id)
    assert event is not None
    assert await _admit(db, event) == Decision.STARTED
    async with db.pool.acquire() as conn:
        await conn.execute("UPDATE zalo_bot_events SET attempt_count=$2 WHERE id=$1", event.id, db.settings.zalo_bot_max_attempts)
    await _expire(db.pool, event)
    assert event.id not in await db.store.due_candidates()
    assert await db.store.claim_event(event.id) is None
    assert await db.store.maintain() == (1, 0)
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status,last_error_code FROM zalo_bot_events WHERE id=$1", event.id)
        assert tuple(row) == ("dead", "CORE_OUTCOME_UNCERTAIN")
        assert await conn.fetchval("SELECT decision FROM zalo_bot_admissions WHERE source_event_key=$1", event.source_event_key) == "started"
        await conn.execute("UPDATE zalo_bot_events SET completed_at=now()-interval '8 days' WHERE id=$1", event.id)
    assert await db.store.maintain() == (0, 1)
    assert await _insert("exhausted", "stable-message") is None
    async with db.pool.acquire() as conn:
        assert await conn.fetchval("SELECT decision FROM zalo_bot_admissions WHERE source_event_key=$1", event.source_event_key) == "started"


# Spawn target must stay module-level for Windows. Each process owns one loop,
# creates a real application pool, and releases every checked-out connection.
def _pooler_worker(url, channel):
    async def run():
        from api.infrastructure.adapters.postgres_zalo_events import PostgresZaloEventStore
        from api.infrastructure.adapters.postgres_zalo_admission import PostgresZaloAdmission
        async with _real_pool(url) as (pool, settings):
            store = PostgresZaloEventStore(settings=settings)
            admission = PostgresZaloAdmission(settings=settings)
            channel.send(("ready", os.getpid()))
            while True:
                command, payload = await asyncio.to_thread(channel.recv)
                if command == "stop":
                    break
                if command == "claim":
                    event = await store.claim_event(payload)
                    channel.send(("claim", event))
                elif command == "admit":
                    decision = await admission.admit(payload.identity_key, payload.source_event_key, event_id=payload.id, claim_token=payload.claim_token)
                    channel.send(("admit", decision.value))
                elif command == "renew":
                    channel.send(("renew", await store.renew_lease(payload)))
                elif command == "release":
                    channel.send(("release", await store.release(payload)))
                elif command == "cache":
                    outcome = {"answer": "stale", "resolution_status": "answered", "project_key": None, "meta": {}}
                    channel.send(("cache", await store.cache_outcome(payload, outcome)))
                elif command == "complete":
                    channel.send(("complete", await store.complete(payload, message_id="stale-provider")))
                elif command == "probe":
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            found = await conn.fetchval("SELECT count(*) FROM zalo_bot_events WHERE id=$1", payload)
                    channel.send(("probe", found, pool.get_idle_size() == pool.get_size()))
    try:
        asyncio.run(run())
    except BaseException as error:
        # Never relay DSN, server error detail, raw SQL arguments or credentials.
        channel.send(("error", type(error).__name__))
        raise SystemExit(1) from None
    finally:
        channel.close()


async def _receive(channel):
    if not await asyncio.to_thread(channel.poll, 20):
        pytest.fail("ZALO_TEST_WORKER_RESPONSE_TIMEOUT", pytrace=False)
    message = channel.recv()
    assert message[0] != "error", message
    return message


async def test_two_process_transaction_pooler_interleaving_restart_and_receipt(database, record_property):
    from api.application.ports.zalo_bot import ZaloAdmissionDecision as Decision
    db = database
    url, approval = _approved_url("pooler")
    # Only mode/reference and masked endpoint are emitted; never credentials.
    target = approval["pooler"]
    record_property("pooler_evidence", json.dumps({"host": "[redacted]", "port": target["port"], "mode": "transaction", "operator_attested": True}))
    await _cutover(db.pool)
    event_id = await _insert("pooler-actor")
    separate = await _insert("pooler-other")
    context = multiprocessing.get_context("spawn")
    workers = []

    async def start():
        parent, child = context.Pipe()
        process = context.Process(target=_pooler_worker, args=(url, child))
        process.start()
        child.close()
        workers.append((process, parent))
        ready = await _receive(parent)
        assert ready[0] == "ready"
        return parent, ready[1]

    try:
        first, pid1 = await start()
        second, pid2 = await start()
        assert pid1 != pid2
        # Probe same sentinel through pooler: approval must map to actual DB.
        for channel in (first, second):
            channel.send(("probe", event_id))
            assert await _receive(channel) == ("probe", 1, True)
        first.send(("claim", event_id))
        second.send(("claim", event_id))
        outcomes = await asyncio.gather(_receive(first), _receive(second))
        assert sum(row[1] is not None for row in outcomes) == 1
        index = next(i for i, row in enumerate(outcomes) if row[1] is not None)
        winner, loser = (first, second) if index == 0 else (second, first)
        old = outcomes[index][1]
        assert old.attempt_count == 1
        async with db.pool.acquire() as conn:
            assert await conn.fetchval("SELECT attempt_count FROM zalo_bot_events WHERE id=$1", event_id) == 1
        loser.send(("claim", separate))
        unrelated = (await _receive(loser))[1]
        assert unrelated is not None
        loser.send(("release", unrelated))
        assert await _receive(loser) == ("release", True)
        winner.send(("admit", old))
        assert await _receive(winner) == ("admit", Decision.STARTED.value)
        async with db.pool.acquire() as conn:
            actor_expiry = await conn.fetchval("SELECT lease_until FROM zalo_bot_actor_leases WHERE identity_key=$1", old.identity_key)
            await conn.execute("UPDATE zalo_bot_events SET claim_token=$2 WHERE id=$1", old.id, uuid4())
        winner.send(("renew", old))
        assert await _receive(winner) == ("renew", None)
        async with db.pool.acquire() as conn:
            assert await conn.fetchval("SELECT lease_until FROM zalo_bot_actor_leases WHERE identity_key=$1", old.identity_key) == actor_expiry
            await conn.execute("UPDATE zalo_bot_events SET claim_token=$2 WHERE id=$1", old.id, UUID(str(old.claim_token)))
        winner.send(("renew", old))
        assert (await _receive(winner))[1] is not None
        for channel in (winner, loser):
            channel.send(("probe", event_id))
            assert await _receive(channel) == ("probe", 1, True)
        # Abrupt process death after STARTED, before outcome: durable source
        # permission spent. New process/pool recovers only ALREADY_STARTED.
        owner_process = next(process for process, channel in workers if channel is winner)
        owner_process.terminate()
        await asyncio.to_thread(owner_process.join, 10)
        assert not owner_process.is_alive()
        await _expire(db.pool, old)
        restarted, new_pid = await start()
        assert new_pid not in {pid1, pid2}
        restarted.send(("claim", event_id))
        recovered = (await _receive(restarted))[1]
        assert recovered is not None and recovered.claim_token != old.claim_token
        restarted.send(("admit", recovered))
        assert await _receive(restarted) == ("admit", Decision.ALREADY_STARTED.value)
        loser.send(("renew", old))
        assert await _receive(loser) == ("renew", None)
        loser.send(("release", old))
        assert await _receive(loser) == ("release", False)
        for command in ("cache", "complete"):
            loser.send((command, old))
            assert await _receive(loser) == (command, False)
        async with db.pool.acquire() as conn:
            assert await conn.fetchval("SELECT admitted_count FROM zalo_bot_burst_windows WHERE identity_key=$1", old.identity_key) == 1
            assert await conn.fetchval("SELECT count(*) FROM zalo_bot_actor_leases WHERE identity_key=$1 AND lease_until>clock_timestamp()", old.identity_key) == 1
    finally:
        for process, channel in workers:
            if process.is_alive():
                channel.send(("stop", None))
                await asyncio.to_thread(process.join, 10)
                if process.is_alive():
                    process.terminate()
                    await asyncio.to_thread(process.join, 10)
            channel.close()
