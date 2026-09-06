"""Cutover smoke: local app runtime -> Supabase pooler (session mode :5432).

Documents the 2026-09-05 runtime cutover. Strictly read-only: version/TLS
check, table counts, and the app-level run_rag_leg retrieval probes. No writes,
no data sync. Credentials come from .env only; the password is never printed.

Usage:
  python scripts/smoke_supabase_runtime.py           # backend = .env as-is (Supabase after cutover)
  python scripts/smoke_supabase_runtime.py --local   # parity reference: applies the commented
                                                     # rollback (local docker) POSTGRES_* values
                                                     # from .env, in-process only (never edits .env)

Output: one JSON report on stdout (redacted connection target, TLS state, table
counts, per-probe chunk counts / degraded flags / doc provenance) so the two
backends can be diffed mechanically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# App-level retrieval probes (issue spec): same questions for both backends.
PROBES = [
    ("camellia", "Camellia có những loại căn hộ nào, diện tích?"),
    ("soleil", "Soleil có những loại căn hộ nào?"),
]
# count(*) only — never SELECT * (columns unused).
COUNT_TABLES = ("documents", "document_chunks", "facts", "images")
ROLLBACK_KEYS = {
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
}


def _drop_inherited_postgres_env() -> None:
    """Remove inherited POSTGRES_* so .env is authoritative for this process."""
    for k in [k for k in os.environ if k.startswith("POSTGRES_")]:
        del os.environ[k]


def apply_local_rollback_env() -> dict[str, str]:
    """Parse the commented rollback POSTGRES_* lines from .env into os.environ.

    In-process only — .env is never edited. MUST run before any api import:
    config.py instantiates Settings (and caches) at import time.
    """
    txt = (_REPO_ROOT / ".env").read_text(encoding="utf-8")
    vals = dict(re.findall(r"^# (POSTGRES_[A-Z_]+)=(.*)$", txt, re.M))
    missing = ROLLBACK_KEYS - set(vals)
    if missing:
        raise SystemExit(f"rollback comment lines missing from .env: {sorted(missing)}")
    _drop_inherited_postgres_env()
    os.environ.update(vals)
    os.environ.pop("POSTGRES_MAX_CONNECTIONS", None)  # keep the Settings default for local
    redacted = {k: ("***" if "PASSWORD" in k else v) for k, v in vals.items()}
    return redacted


def apply_dsn_quote_workaround() -> None:
    """IN-PROCESS ONLY: quote the password inside build_dsn()'s output.

    WHY: asyncpg splits a DSN netloc on the FIRST '@'
    (asyncpg/connect_utils.py _parse_connect_dsn_and_args:
    ``dsn_auth, _, dsn_hostspec = parsed.netloc.partition('@')``), while libpq
    and urllib split at the LAST one. The Supabase DB password contains '@',
    so with the raw password every build_dsn()-based create_pool site fails
    with ``gaierror 11003`` on a corrupted hostspec (NOT a TLS failure).
    asyncpg percent-DECODES the DSN password (unquote in the same function),
    so quoting the password fixes every app pool and does NOT affect LightRAG,
    which passes the env password to asyncpg as a raw kwarg (no decoding).

    PRODUCTION FIX — landed in api/application/services/sql_leg.py build_dsn()
    (and in api/infrastructure/config/config.py pg_dsn property). The in-process
    monkeypatch below is kept only as a fallback for pre-fix environments and
    for environments where the build_dsn fix is not yet available.
    """
    import api.application.services.sql_leg as sql_leg

    original = sql_leg.build_dsn

    def quoted_dsn() -> str:
        # urlsplit parses the raw-password DSN at the LAST '@' (host verified
        # correct in diagnostics); re-emit with the password percent-encoded.
        parts = urlsplit(original())
        return (
            f"postgresql://{parts.username}:{quote(unquote(parts.password or ''), safe='')}"
            f"@{parts.hostname}:{parts.port}{parts.path}"
        )

    sql_leg.build_dsn = quoted_dsn


async def probe_backend() -> dict:
    """App-config-driven, read-only probe via the app's own pool + rag_leg path."""
    # Imported AFTER env decisions: config reads .env + os.environ at import time.
    from api import get_cfg
    from api.application.services.rag_leg import run_rag_leg
    from api.application.services.sql_leg import close_ro_pool, get_ro_pool
    from api.infrastructure.config.config import get_settings

    settings = get_settings()
    apply_dsn_quote_workaround()
    pool = await get_ro_pool()  # app pool path: build_dsn() + _pooler_safe_kwargs()

    report: dict = {}
    async with pool.acquire() as conn:
        ver = await conn.fetchval("SELECT version()")
        ssl_row = await conn.fetchrow(
            "SELECT ssl, version AS tls_version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
        )
        counts = {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in COUNT_TABLES}
        # Client-leg TLS: pg_stat_ssl only shows the server-side leg, which
        # through Supavisor is the pooler->postgres hop, not client->pooler.
        try:
            transport = conn._protocol.transport  # noqa: SLF001 — diagnostic only
            client_tls = "TLS" if transport.get_extra_info("sslcontext") else "plaintext"
        except Exception:  # noqa: BLE001 — introspection is best-effort
            client_tls = "unknown"
    report["server"] = (ver or "").split(" on ")[0]
    report["tls_server_leg"] = bool(ssl_row["ssl"]) if ssl_row else None
    report["tls_server_leg_version"] = ssl_row["tls_version"] if ssl_row else None
    report["tls_client_leg"] = client_tls
    report["counts"] = {t: int(c) for t, c in counts.items()}

    # Connection target as the app resolves it (redacted: no user or password).
    report["settings_dsn_target"] = {
        "host": f"{get_cfg('postgres_host')}:{int(get_cfg('postgres_port', 5432))}",
        "database": get_cfg("postgres_database"),
        "pool_max": int(get_cfg("postgres_max_connections", 10) or 10),
        "statement_cache_size": 0,  # _pooler_safe_kwargs applied at every create_pool site
    }

    probes = []
    for project, question in PROBES:
        res = await run_rag_leg(question, hl=[], ll=[], as_of=date.today(), project_key=project)
        chunks = res.chunks or []
        probes.append(
            {
                "project": project,
                "question": question,
                "degraded": res.degraded,
                "error": res.error,
                "degraded_reasons": list(res.degraded_reasons),
                "chunk_count": len(chunks),
                "doc_ids": sorted(
                    {str(c.get("file_path") or c.get("id")) for c in chunks if c.get("file_path") or c.get("id")}
                )[:12],
                "top_scores": [round(float(c.get("score", 0) or 0), 4) for c in chunks[:5]],
                "first_chunk_excerpt": (chunks[0]["content"][:160] if chunks else None),
            }
        )
    report["probes"] = probes
    report["rag_query_mode"] = settings.rag_query_mode

    await close_ro_pool()
    return report


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--local",
        action="store_true",
        help="run against the commented local-docker rollback values from .env (parity reference)",
    )
    args = ap.parse_args()

    if args.local:
        applied = apply_local_rollback_env()
        print(
            f"backend=local-docker (rollback env applied in-process: "
            f"host={applied['POSTGRES_HOST']}:{applied.get('POSTGRES_PORT', '5432')} "
            f"db={applied['POSTGRES_DATABASE']})",
            file=sys.stderr,
        )
    else:
        _drop_inherited_postgres_env()

    print(
        "NOTE: in-process DSN-quote workaround ACTIVE as fallback (build_dsn and "
        "Settings.pg_dsn now quote the password in-code). No file was modified; "
        "see apply_dsn_quote_workaround docstring.",
        file=sys.stderr,
    )
    rep = await probe_backend()
    rep["workaround_dsn_quote_in_process"] = True
    rep["backend"] = "local-docker" if args.local else "supabase-pooler-session"
    print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
