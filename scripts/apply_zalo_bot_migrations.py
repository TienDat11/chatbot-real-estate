"""One-off Zalo production DB cutover operator (6-stage, dry-runnable).

Applies the ZB-03 Zalo Bot production migrations in strict order:

    1. db/migrations/2026-09-17-zalo-bot-events.sql
    2. db/migrations/2026-09-17-zalo-bot-roundtrip.sql
    3. db/migrations/2026-09-17-zalo-bot-hardening.sql
    4. scripts/backfill_zalo_bot_hardening.py  (backfill — Zalo MUST be disabled)
    5. db/migrations/2026-09-17-zalo-bot-hardening-indexes.sql  (CONCURRENTLY split)
    6. db/migrations/2026-09-17-zalo-bot-hardening-finalize.sql

psql is NOT installed on this host, so this Python script is the only viable
mechanism. It mirrors the EXACT migration semantics proven in the test suite at
``tests/test_zalo_bot_postgres.py`` function ``_migration``:

- Index migrations use the split path (CREATE INDEX CONCURRENTLY is illegal
  inside a transaction block in PostgreSQL):
    preflight, separator, indexes = sql.partition("$preflight$;")
    await conn.execute(preflight + separator)   # one command
    for statement in indexes.split(";"):          # each CONCURRENTLY separately
        if statement.strip(): await conn.execute(statement)
- All other migrations execute as a single ``conn.execute(sql)``.

Single-source-of-truth DSN: the resolved BOM-cleaned DSN is computed ONCE via
``_resolve_dsn()`` and used to build ONE pool that feeds every stage AND verify.
It does NOT delegate to ``get_lead_pool()`` (which builds its own DSN via
``build_dsn()`` — a different source, producing a dangerous split-brain target
where the printed target could differ from the target actually mutated, and
where the ``ZALO_MIGRATION_DSN`` override and BOM-stripping would be silently
ignored).

The pool is built with ``_pooler_safe_kwargs()`` (statement_cache_size=0,
max_cached_statement_lifetime=0, max_cacheable_statement_size=0) because the
Supabase pooler / Supavisor does not support prepared statements; these kwargs
are harmless on direct :5432 connections (the client simply skips prepare
round-trips and inlines the SQL).

Safety gates (all fail closed):
- Refuse to run if ``--stage`` is missing or out of range 1..6.
- Refuse unless env ``ZALO_MIGRATION_APPROVED=1`` is set.
- DSN: primary path is ``get_settings().pg_dsn_sync`` (the app's own settings).
  ``ZALO_MIGRATION_DSN`` is an optional explicit override that wins if set.
  A leading UTF-8 BOM (``\\ufeff``) on the password is stripped — the prod
  password genuinely carries one and that broke the naive probe.
- Print the resolved target ONCE at start (host / port / database / user,
  NO password) so the operator can eyeball it before anything runs.
- ``--dry-run``: resolve everything, print the target + ordered stage list,
  open NO connection, exit 0.

Windows DNS quirk: asyncpg's loop_resolver has failed on this host for the
Supabase pooler hostname even though the OS resolver worked. asyncpg.connect
accepts a ``host`` kwarg that may be an IP; we pre-resolve the hostname to an
IP via ``socket.getaddrinfo`` (the OS resolver) and pass the IP as ``host``
ONLY when sslmode is not ``verify-full``/``verify-ca`` (those require hostname
verification, which would fail for an IP). The DSN is always passed as the
first positional arg to asyncpg so asyncpg owns user/password/database/sslmode;
we override ONLY host (and restate port) via ``_safe_overrides()``. This was
empirically verified via asyncpg.connect_utils._parse_connect_arguments:
passing ``host`` suppresses DSN netloc parsing (host+port only), so the
port must be restated. user/password/database/sslmode come from the DSN
unaffected. ``scheme`` and ``server_hostname`` kwargs are REJECTED by
asyncpg.connect (TypeError), so they are NOT used.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import asyncpg

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Pooler-safe kwargs from sql_leg — Supavisor/pooler rejects prepared
# statements; harmless on direct :5432. (global_assistant_service.py
# previously had a syntax error blocking the api import chain; now resolved.)
from api.application.services.sql_leg import _pooler_safe_kwargs  # noqa: E402
from api.infrastructure.config.config import get_settings  # noqa: E402
from scripts.backfill_zalo_bot_hardening import backfill  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db/migrations"

_BOM = "\ufeff"

# Stage order is a hard constraint: migrations precede the backfill, and the
# backfill MUST run with Zalo disabled (backfill() raises ZALO_MUST_BE_DISABLED
# when get_settings().zalo_bot_enabled is true).
STAGES = [
    (1, "2026-09-17-zalo-bot-events.sql", "migration"),
    (2, "2026-09-17-zalo-bot-roundtrip.sql", "migration"),
    (3, "2026-09-17-zalo-bot-hardening.sql", "migration"),
    (4, "scripts/backfill_zalo_bot_hardening.py", "backfill"),
    (5, "2026-09-17-zalo-bot-hardening-indexes.sql", "migration-split"),
    (6, "2026-09-17-zalo-bot-hardening-finalize.sql", "migration"),
]


class MigrationError(Exception):
    """Raised on any fail-closed gate or migration failure."""


def _strip_bom_password(dsn: str) -> str:
    """Strip a leading UTF-8 BOM from the password component of a DSN.

    The prod password value genuinely carries a BOM (``\\ufeff``) when read
    from certain env sources; asyncpg does not strip it, which breaks auth.
    We percent-encode defensively so the BOM is always stripped from the raw
    secret before it reaches the driver.
    """
    parts = urlsplit(dsn)
    if not parts.password:
        return dsn
    raw_pw = unquote(parts.password)
    if raw_pw.startswith(_BOM):
        raw_pw = raw_pw[len(_BOM):]
        encoded_pw = quote(raw_pw, safe="")
        netloc = f"{parts.username}:{encoded_pw}@{parts.hostname}"
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )
    return dsn


def _redact_dsn(dsn: str) -> str:
    """Return a DSN with the password replaced by ``***`` for safe logging."""
    parts = urlsplit(dsn)
    scheme = parts.scheme
    host = parts.hostname or "?"
    port = f":{parts.port}" if parts.port is not None else ""
    database = parts.path.lstrip("/") or "?"
    user = parts.username or "?"
    return f"{scheme}://{user}:***@{host}{port}/{database}"


def _target_components(dsn: str) -> dict[str, str]:
    """Extract redactable target components from a DSN."""
    parts = urlsplit(dsn)
    return {
        "host": parts.hostname or "?",
        "port": str(parts.port) if parts.port is not None else "?",
        "database": parts.path.lstrip("/") or "?",
        "user": parts.username or "?",
    }


def _resolve_dsn() -> str:
    """Resolve the migration DSN.

    Primary path: the app's own ``get_settings().pg_dsn_sync`` so the target
    is identical to what the application uses — no baked-in host/port/user/
    database anywhere in this script.

    Optional override: if env ``ZALO_MIGRATION_DSN`` is set, it wins.
    """
    settings = get_settings()
    dsn = os.environ.get("ZALO_MIGRATION_DSN")
    if dsn:
        return _strip_bom_password(dsn)
    return _strip_bom_password(settings.pg_dsn_sync)


def _dsn_sslmode(dsn: str) -> str | None:
    """Extract sslmode from the DSN query string (if present)."""
    parts = urlsplit(dsn)
    if not parts.query:
        return None
    params = parse_qs(parts.query, keep_blank_values=True)
    vals = params.get("sslmode", [])
    return vals[0].lower() if vals else None


def _pre_resolve_host(dsn: str) -> str | None:
    """Pre-resolve the DSN host to an IP to work around the Windows DNS quirk.

    asyncpg's loop_resolver has failed on this host for the Supabase pooler
    hostname even though the OS resolver worked. asyncpg.connect's ``host``
    kwarg accepts a literal IP, so we pre-resolve via ``socket.getaddrinfo``
    (the OS resolver) and return the IP. If resolution fails, we return the
    original hostname and let asyncpg attempt its own resolution.

    NOTE: ``scheme`` and ``server_hostname`` kwargs were gate-tested against
    asyncpg.connect and REJECTED (TypeError), so they are NOT used here.
    """
    parts = urlsplit(dsn)
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        if infos:
            return infos[0][4][0]
    except socket.gaierror:
        pass
    return host


def _safe_overrides(dsn: str) -> dict[str, Any]:
    """Build asyncpg.create_pool overrides: only host+port when IP pre-resolve differs.

    KEY INSIGHT (verified via asyncpg.connect_utils._parse_connect_arguments
    gate test): passing ``host`` as a kwarg causes asyncpg to SKIP parsing the
    DSN **netloc (host+port)** — but ONLY host+port are affected. user,
    password, database, and sslmode continue to come from the DSN unaffected.
    The DSN's **port** is silently lost (probe P2: port defaults to 5432 when
    ``host`` is overridden but ``port`` is not restated), so the **port** must
    be restated. To keep user/password/database/sslmode as the single source of
    truth, we pass the DSN as the FIRST positional argument to ``create_pool``
    and override ONLY ``host`` (and restate ``port``) via ``_safe_overrides``.

    When sslmode is verify-full or verify-ca, hostname verification is required
    (check_hostname = sslmode >= verify_full per asyncpg line ~418), so
    connecting by IP would FAIL the TLS handshake. In that case, skip
    pre-resolution entirely and let asyncpg use the DSN hostname.
    """
    sslmode = _dsn_sslmode(dsn)
    if sslmode in ("verify-full", "verify-ca"):
        # Hostname verification required — do NOT override host; let asyncpg
        # parse the DSN hostname normally.
        return {}
    original_host = urlsplit(dsn).hostname or "localhost"
    resolved = _pre_resolve_host(dsn)
    overrides: dict[str, Any] = {}
    if resolved != original_host:
        # Only override when pre-resolution actually changed the host.
        # Port MUST be restated because overriding `host` suppresses the DSN
        # netloc parse (including its port).
        parts = urlsplit(dsn)
        overrides["host"] = resolved
        overrides["port"] = parts.port or 5432
    return overrides


async def _create_pool(dsn: str) -> asyncpg.Pool:
    """Build ONE pool from the resolved, BOM-cleaned DSN.

    The DSN is passed as the first positional arg so asyncpg owns
    user/password/database/sslmode (the single source of truth). ONLY ``host``
    and ``port`` are overridden via ``_safe_overrides`` when IP pre-resolution
    applies (non-verify-full/verify-ca sslmode). Uses ``_pooler_safe_kwargs()``
    (statement_cache_size=0, etc.) — required because Supabase pooler /
    Supavisor rejects prepared statements; harmless on direct :5432.
    """
    overrides = _safe_overrides(dsn)
    overrides.update(_pooler_safe_kwargs())
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5, **overrides)


async def _apply_migration(pool, name: str, split: bool) -> int:
    """Apply a migration file, mirroring _migration in the test suite exactly.

    Returns the number of statements executed (for reporting).
    """
    sql = (MIGRATIONS / name).read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        if split:
            # Create INDEX CONCURRENTLY is illegal inside a transaction block.
            # Preserve the dollar-quoted preflight as one command; each
            # subsequent CONCURRENTLY statement remains its own top-level command.
            preflight, separator, indexes = sql.partition("$preflight$;")
            assert separator, f"index migration preflight delimiter missing in {name}"
            await conn.execute(preflight + separator)
            executed = 1
            for statement in indexes.split(";"):
                stmt = statement.strip()
                if stmt:
                    await conn.execute(stmt)
                    executed += 1
            return executed
        else:
            await conn.execute(sql)
            return 1


async def _apply_stage(pool, stage_spec: tuple[int, str, str]) -> int:
    """Run exactly one stage. Returns a count meaningful for that stage."""
    stage_num, target, kind = stage_spec
    start = time.monotonic()
    if kind == "backfill":
        # backfill() raises ZALO_MUST_BE_DISABLED when zalo_bot_enabled is true.
        secret = os.environ.get("ZALO_BOT_IDENTITY_SECRET", "")
        if not secret:
            raise MigrationError(
                "ZALO_BOT_IDENTITY_SECRET must be set for stage 4 (backfill)"
            )
        changed = await backfill(pool, secret)
        elapsed = time.monotonic() - start
        print(
            f"stage={stage_num} target=backfill_zalo_bot_hardening "
            f"changed_rows={changed} elapsed_s={elapsed:.3f}"
        )
        return changed
    else:
        split = kind == "migration-split"
        executed = await _apply_migration(pool, target, split)
        elapsed = time.monotonic() - start
        print(
            f"stage={stage_num} target={target} statements={executed} "
            f"elapsed_s={elapsed:.3f}"
        )
        return executed


async def _run_stage(args, pool) -> int:
    """Execute a single stage with safety gates applied."""
    stage_num = args.stage
    if not (1 <= stage_num <= 6):
        raise MigrationError(f"--stage must be in range 1..6, got {stage_num}")
    stage_spec = STAGES[stage_num - 1]
    return await _apply_stage(pool, stage_spec)


async def _run_verify(dsn: str) -> None:
    """Read-only post-checks: confirm indexes exist and NOT NULL constraints applied.

    Uses the SAME resolved DSN and SAME pool path as the stages — no second
    connection strategy.
    """
    settings = get_settings()
    # For verify, we check that Zalo is disabled (backfill safety).
    if settings.zalo_bot_enabled:
        raise MigrationError(
            "zalo_bot_enabled must be False for verification (Zalo writers "
            "must be stopped)"
        )

    pool = await _create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            # Read-only checks: indexes present and valid, columns NOT NULL.
            indexes = await conn.fetch(
                """
                SELECT indexname, indisvalid
                FROM pg_indexes i
                JOIN pg_index idx ON idx.indexrelid = i.indexname::regclass
                WHERE i.tablename = 'zalo_bot_events'
                  AND indexname IN ('zalo_bot_events_ingress_key_uidx',
                    'zalo_bot_events_source_key_uidx',
                    'zalo_bot_events_due_idx',
                    'zalo_bot_events_terminal_idx')
                ORDER BY 1
                """
            )
            print(f"verify: indexes={len(indexes)}/4")
            for row in indexes:
                print(f"  {row['indexname']} valid={row['indisvalid']}")

            cols = await conn.fetch(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'zalo_bot_events'
                  AND column_name IN ('ingress_key', 'identity_key',
                    'source_event_key')
                ORDER BY 1
                """
            )
            print(f"verify: not_null_cols={len(cols)}/3")
            for row in cols:
                print(f"  {row['column_name']} nullable={row['is_nullable']}")
    finally:
        await pool.close()


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Zalo Bot ZB-03 production DB cutover (6-stage, dry-runnable)"
    )
    parser.add_argument(
        "--stage", type=int, choices=range(1, 7),
        help="Run exactly one stage (1..6)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Run read-only post-checks on the resolved target",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve target + stages only; open NO connection",
    )
    args = parser.parse_args()

    # Fail-closed gate: approval required.
    if os.environ.get("ZALO_MIGRATION_APPROVED", "") != "1":
        print(
            "REFUSING: ZALO_MIGRATION_APPROVED is not set to 1. "
            "Set ZALO_MIGRATION_APPROVED=1 to proceed.",
            file=sys.stderr,
        )
        return 2

    # Fail-closed gate: stage is required (unless --verify or --dry-run).
    if args.stage is None and not args.verify and not args.dry_run:
        print(
            "REFUSING: --stage N (1..6) is required. Use --dry-run to preview.",
            file=sys.stderr,
        )
        return 2

    # Resolve the DSN ONCE — this is the single source of truth for all paths.
    dsn = _resolve_dsn()
    target = _target_components(dsn)

    # Print the resolved target ONCE at start (NO password).
    print(
        f"target: host={target['host']} port={target['port']} "
        f"database={target['database']} user={target['user']}"
    )

    if args.dry_run:
        print("dry_run=true — no connection opened")
        print("ordered stages:")
        for num, fname, kind in STAGES:
            print(f"  stage {num}: {fname} ({kind})")
        return 0

    if args.verify:
        await _run_verify(dsn)
        return 0

    # Execute the requested single stage via ONE pool built from the resolved DSN.
    pool = await _create_pool(dsn)
    try:
        if args.stage is not None:
            await _run_stage(args, pool)
        else:
            # Should not reach here — stage is required.
            print("REFUSING: --stage N is required.", file=sys.stderr)
            return 2
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
