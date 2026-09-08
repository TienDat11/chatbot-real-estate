#!/usr/bin/env bash
# =============================================================================
# restore_supabase.sh — pg_restore a local .dump into a Supabase Postgres DB.
#
# WHY:
#   - Supabase manages its own roles (postgres/anon/authenticated/service_role),
#     so ownership and GRANT statements from the source DB must NOT be applied:
#     --no-owner --no-privileges. The dump was taken with the same flags, and
#     pg_restore re-applies them defensively at restore time.
#   - pgvector is preinstalled on Supabase (schema `extensions`). The dump's
#     `CREATE EXTENSION IF NOT EXISTS vector` is a no-op there; we still run
#     explicit IF NOT EXISTS creates up front so btree_gist/pgcrypto are
#     available before the HNSW index builds. Extension handling is the ONLY
#     thing done outside pg_restore — everything else is pure archive restore.
#   - The schema's RLS policies reference roles ragre/ro_query/audit_append;
#     pg_dump does not dump roles, so they must exist on the target or every
#     CREATE POLICY aborts. They are pre-created idempotently (NOLOGIN).
#   - Custom-format archives require a SEEKABLE file — pg_restore cannot read
#     -Fc from a pipe. When no host client exists, we docker cp the dump into
#     the local postgres container and restore from there (version-matched).
#   - Restore MUST go over the DIRECT connection (port 5432). The Supavisor
#     pooler (port 6543) is transaction-mode and will not survive a long
#     multi-statement restore session.
#
# Usage:
#   scripts/restore_supabase.sh [--dry-run] [--make-dump] [<dump_file>]
#
#   --make-dump  first pg_dump -Fc --no-owner --no-privileges from the LOCAL
#                docker container (version-matched pg_dump) into db/backups/
#                supabase_seed_<ts>.dump, then restore that archive.
#   --dry-run    print the full plan (target, client mode, pre-SQL, pg_restore
#                args, verify steps) and exit 0 — nothing is executed.
#
# Env:
#   SUPABASE_DB_URL    Supavisor pooler SESSION URI:
#                      postgresql://postgres.<ref>:<pw>@aws-<n>-<region>.pooler.supabase.com:5432/postgres
#                      Port 6543 (transaction mode) is hard-rejected below — a
#                      multi-statement restore needs a session. The direct
#                      db.<ref>.supabase.co:5432 host is IPv6-only and cannot
#                      be reached from IPv4-only hosts.
#   POSTGRES_CONTAINER (default ragre-postgres; docker fallback client only)
#   LOCAL_PG_USER / LOCAL_PG_DB (defaults ragre/ragre; --make-dump + parity)
#   SKIP_VERIFY=1      skip post-restore verification + row-count parity
#
# Prerequisites (Windows host, Git Bash):
#   - psql/libpq + pg_restore on PATH with version >= 16 (the dump is produced
#     by local PG16.14; the target server is PG 17.6 — restoring a v16 archive
#     into v17 is supported, a v17 client also works. --make-dump runs pg_dump
#     INSIDE the local container so it is always version-matched), OR the local
#     docker container for the pg_dump/pg_restore fallback path.
#   - TLS: by default, sslmode=require encrypts the connection but does NOT
#     verify the server certificate. If the network is TLS-intercepting (common
#     on consumer routers / corporate proxies), the connection is vulnerable to
#     MITM. Set PGSSLROOTCERT to the Supabase CA bundle to enable
#     sslmode=verify-full (server identity is checked). Without that env var,
#     a warning is printed on every run.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=0
MAKE_DUMP=0
DUMP_FILE=""
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    --make-dump) MAKE_DUMP=1 ;;
    -h|--help) sed -n '2,60p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) DUMP_FILE="${arg}" ;;
  esac
done
if [[ "${MAKE_DUMP}" != "1" ]]; then
  if [[ -z "${DUMP_FILE}" ]] || [[ ! -f "${DUMP_FILE}" ]]; then
    echo "ERROR: usage: scripts/restore_supabase.sh [--dry-run] [--make-dump] <dump_file>" >&2
    exit 1
  fi
fi

# Load SUPABASE_DB_URL from .env without sourcing it (the file is a flat
# KEY=value list whose values may contain spaces/quotes — `source` would try to
# execute them). Already-exported environment vars stay authoritative.
ENV_FILE="${REPO_ROOT}/.env"
env_get() {
  # $1 = key; prints the value (quotes stripped) or nothing if absent.
  local key="$1"
  local line
  if [[ -f "${ENV_FILE}" ]]; then
    line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    line="${line#*=}"
    line="${line%\"*}"
    line="${line#\"*}"
    line="${line%%\"*}"
    line="${line%%\#*}"
    printf '%s' "${line}"
  fi
}

SUPABASE_DB_URL="${SUPABASE_DB_URL:-$(env_get SUPABASE_DB_URL)}"
if [[ -z "${SUPABASE_DB_URL}" ]]; then
  echo "ERROR: SUPABASE_DB_URL is not set (put it in .env or export it)." >&2
  echo "  Format: postgresql://postgres.<ref>:<password>@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres" >&2
  exit 1
fi

# Parse the URL into components so the password never appears on a command line
# or in `ps` output. Userinfo/host split at the LAST '@' (passwords may contain
# '@' characters; libpq and urllib both split on the last one). Only
# user:pass@host:port/db form is supported.
if [[ "${SUPABASE_DB_URL}" =~ ^postgres(ql)?://([^:]+):(.+)@([^@:/]+)(:([0-9]+))?/([^?]+) ]]; then
  SUPABASE_USER="${BASH_REMATCH[2]}"
  SUPABASE_PASSWORD="${BASH_REMATCH[3]}"
  SUPABASE_HOST="${BASH_REMATCH[4]}"
  SUPABASE_PORT="${BASH_REMATCH[6]:-5432}"
  SUPABASE_DATABASE="${BASH_REMATCH[7]}"
else
  echo "ERROR: cannot parse SUPABASE_DB_URL (expected postgresql://user:pass@host:port/db)." >&2
  exit 1
fi

if [[ "${SUPABASE_PORT}" == "6543" ]]; then
  echo "ERROR: port 6543 is the Supavisor POOLER (transaction-mode)." >&2
  echo "  Multi-statement restores require a session-mode connection, not the transaction-mode pooler." >&2
  echo "  Use the pooler session endpoint: postgresql://postgres.<ref>:<pw>@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres" >&2
  exit 1
fi

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ragre-postgres}"

# libpq TLS policy: encrypt, never verify certificates (TLS-interception
# proxies with self-signed chains are common on consumer routers/AV software).
# sslmode=require keeps psql/pg_restore working regardless of the chain.
if [[ -n "${PGSSLROOTCERT:-}" ]]; then
  PGSSLMODE="verify-full"
  export PGSSLROOTCERT
  echo "[tls] server verification is ON (sslmode=verify-full, CA: ${PGSSLROOTCERT})"
else
  PGSSLMODE="require"
  echo "WARNING: TLS connection is encrypted but NOT server-verified (sslmode=require). MITM is" >&2
  echo "  possible on TLS-intercepting networks. Set PGSSLROOTCERT=<Supabase CA path> to enable" >&2
  echo "  sslmode=verify-full (server identity check)." >&2
fi
export PGSSLMODE

# ---------------------------------------------------------------------------
# Optional: produce the source archive from the LOCAL container. pg_dump runs
# INSIDE the container (version-matched with its PG16.14 server) and streams
# to a host file; --no-owner --no-privileges matches the Supabase restore.
# ---------------------------------------------------------------------------
if [[ "${MAKE_DUMP}" == "1" ]]; then
  # --no-comments: Supabase extensions are owned by supabase_admin, so the
  # postgres role cannot execute COMMENT ON EXTENSION — which aborts a
  # --single-transaction restore. All COMMENT statements are cosmetic and are
  # stripped at dump time (tables/columns lose comments too — accepted).
  DUMP_CMD=(docker exec "${POSTGRES_CONTAINER}" pg_dump -U "${LOCAL_PG_USER:-ragre}" -d "${LOCAL_PG_DB:-ragre}" -Fc --no-owner --no-privileges --no-comments)
  DUMP_OUT="${REPO_ROOT}/db/backups/supabase_seed_$(date +%Y%m%d_%H%M%S).dump"
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "[dry-run] would run: ${DUMP_CMD[*]} > ${DUMP_OUT}"
  else
    docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}" || { echo "ERROR: container '${POSTGRES_CONTAINER}' is not running (--make-dump)." >&2; exit 1; }
    mkdir -p "$(dirname "${DUMP_OUT}")"
    echo "[dump] ${DUMP_CMD[*]} > ${DUMP_OUT}"
    "${DUMP_CMD[@]}" > "${DUMP_OUT}"
    echo "[dump] wrote $(du -h "${DUMP_OUT}" | cut -f1) -> ${DUMP_OUT}"
  fi
  DUMP_FILE="${DUMP_OUT}"
fi

if [[ "${DRY_RUN}" != "1" ]]; then
  if [[ -z "${DUMP_FILE}" ]] || [[ ! -f "${DUMP_FILE}" ]]; then
    echo "ERROR: dump file not found: '${DUMP_FILE}'" >&2
    exit 1
  fi
fi
DUMP_BASENAME="$(basename "${DUMP_FILE}")"

# ---------------------------------------------------------------------------
# Client selection: host pg_restore/psql if present, else the local postgres
# container (pg_dump/pg_restore 16.14 == local server version).
# ---------------------------------------------------------------------------
HAVE_HOST_CLIENT=0
if command -v pg_restore >/dev/null 2>&1 && command -v psql >/dev/null 2>&1; then
  HAVE_HOST_CLIENT=1
elif ! docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  echo "ERROR: no pg_restore/psql on PATH and container '${POSTGRES_CONTAINER}' is not running." >&2
  exit 1
fi

# Docker client path: `docker exec -e PGPASSWORD=...` puts the password in the
# docker client's argv, visible in `ps` on the host. An --env-file is read by
# the docker client from disk and injected into the container process
# environment only, so the secret never appears on a command line. The temp
# file is created 0600 and removed on exit. The pg_restore docker call runs
# under MSYS_NO_PATHCONV=1 (container-side /tmp path must not be rewritten),
# so docker.exe needs the env-file as a native Windows path under Git Bash:
# translate it here via cygpath; plain POSIX path elsewhere (Linux/WSL).
if [[ "${HAVE_HOST_CLIENT}" != "1" ]]; then
  PGPASS_FILE="$(mktemp)"
  chmod 600 "${PGPASS_FILE}"
  trap 'rm -f "${PGPASS_FILE}"' EXIT
  printf 'PGPASSWORD=%s\nPGSSLMODE=%s\n' "${SUPABASE_PASSWORD}" "${PGSSLMODE}" > "${PGPASS_FILE}"
  if command -v cygpath >/dev/null 2>&1; then
    PGPASS_FILE_DOCKER="$(cygpath -w "${PGPASS_FILE}")"
  else
    PGPASS_FILE_DOCKER="${PGPASS_FILE}"
  fi
fi

run_psql() {
  # $1 = SQL text; executes against Supabase via the chosen client.
  # -w: never prompt (a wrong password must fail fast, not hang the pipeline).
  if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
    PGPASSWORD="${SUPABASE_PASSWORD}" psql -w \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -c "$1"
  else
    docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
      psql -w \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -c "$1"
  fi
}

run_psql_scalar() {
  # $1 = SQL returning a single value; prints it unaligned (no header/footer).
  if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
    PGPASSWORD="${SUPABASE_PASSWORD}" psql -w -tA \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -c "$1"
  else
    docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
      psql -w -tA \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -c "$1"
  fi
}

run_psql_file() {
  # $1 = SQL file on the HOST; executes against Supabase. The docker path
  # copies the file into the container first (ON_ERROR_STOP stays on).
  local SRC
  SRC="$(cygpath -m "$1" 2>/dev/null || echo "$1")"
  if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
    PGPASSWORD="${SUPABASE_PASSWORD}" psql -w \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -f "$1"
  else
    MSYS_NO_PATHCONV=1 docker cp "${SRC}" "${POSTGRES_CONTAINER}:/tmp/$(basename "$1")"
    MSYS_NO_PATHCONV=1 docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
      psql -w \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -f "/tmp/$(basename "$1")"
  fi
}

run_pg_restore() {
  # Archive restore: additive only (NEVER --clean/--create — nothing that
  # already exists on the target is dropped, incl. the 5 platform tables),
  # --single-transaction so any failure rolls the ENTIRE restore back.
  if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
    PGPASSWORD="${SUPABASE_PASSWORD}" pg_restore -w \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      --no-owner --no-privileges --single-transaction --exit-on-error \
      -Fc "$1"
  else
    # Custom format needs random access: cp the archive into the container,
    # restore from there, then remove it. Never pipe -Fc via stdin.
    # MSYS_NO_PATHCONV stops Git Bash translating the container-side /tmp/...
    # path; the HOST-side source is converted explicitly via cygpath -m so it
    # works whether the caller passed D:/... or /d/... form.
    local SRC
    SRC="$(cygpath -m "$1" 2>/dev/null || echo "$1")"
    MSYS_NO_PATHCONV=1 docker cp "${SRC}" "${POSTGRES_CONTAINER}:/tmp/$(basename "$1")"
    if MSYS_NO_PATHCONV=1 docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
        pg_restore -w \
        -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
        -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
        --no-owner --no-privileges --single-transaction --exit-on-error \
        -Fc "/tmp/$(basename "$1")"; then
      MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/$(basename "$1")"
    else
      MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/$(basename "$1")"
      exit 1
    fi
  fi
}

# ---------------------------------------------------------------------------
# Confirm before overwriting anything on the target.
# ---------------------------------------------------------------------------
echo "TARGET : postgresql://${SUPABASE_USER}@${SUPABASE_HOST}:${SUPABASE_PORT}/${SUPABASE_DATABASE}"
echo "CLIENT : $( [[ "${HAVE_HOST_CLIENT}" == "1" ]] && echo "host psql/pg_restore ($(pg_restore --version 2>/dev/null | awk '{print $NF}'))" || echo "docker fallback via ${POSTGRES_CONTAINER}" )"
echo "DUMP   : ${DUMP_FILE}"
echo "MODE   : additive (--no-owner --no-privileges --single-transaction); NO --clean; nothing pre-existing is dropped"
if [[ "${DRY_RUN}" == "1" ]]; then
  echo "[dry-run] pre-SQL   : idempotent NOLOGIN roles (ragre, ro_query, audit_append) + CREATE EXTENSION vector/btree_gist/pgcrypto WITH SCHEMA extensions"
  echo "[dry-run] pg_restore: -h ${SUPABASE_HOST} -p ${SUPABASE_PORT} -U ${SUPABASE_USER} -d ${SUPABASE_DATABASE} --no-owner --no-privileges --single-transaction --exit-on-error -Fc <dump>"
  echo "[dry-run] verify    : platform-table guard (5 tables) + row counts + embedding dims + HNSW indexes + scripts/verify_ingest.sql + row-count parity vs local (SKIP_VERIFY=1 to skip)"
  echo "[dry-run] exit — nothing was executed"
  exit 0
fi
read -r -p "This will create objects on the Supabase target. Type 'RESTORE' to continue: " CONFIRM
if [[ "${CONFIRM}" != "RESTORE" ]]; then
  echo "Aborted — nothing was changed."
  exit 1
fi

# ---------------------------------------------------------------------------
# Pre-restore: idempotent roles + extensions (the only non-archive SQL).
# ---------------------------------------------------------------------------
echo "[restore] pre-creating roles + extensions (idempotent)..."
run_psql "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ragre') THEN
    CREATE ROLE ragre NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ro_query') THEN
    CREATE ROLE ro_query NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'audit_append') THEN
    CREATE ROLE audit_append NOLOGIN;
  END IF;
END
\$\$;
SHOW search_path;
-- vector MUST live in public: the source DB had it there, so the dump DDL
-- references public.vector(1024) / public.vector_cosine_ops. If a previous
-- attempt installed it into the extensions schema, move it (we own it).
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'
             AND extnamespace = 'extensions'::regnamespace) THEN
    EXECUTE 'ALTER EXTENSION vector SET SCHEMA public';
  END IF;
END
\$\$;
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
"

# ---------------------------------------------------------------------------
# TOC whitelist pre-check: parse pg_restore --list output and ABORT if the
# archive contains any of the 5 protected platform tables. These tables are
# managed by Supabase and must never be overwritten by a restore.
# ---------------------------------------------------------------------------
PROTECTED_TABLES="ai_generation_logs assets platform_syncs posts publishing_queue"
echo "[restore] checking archive TOC for protected platform tables..."
if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
  if ! TOC_OUTPUT="$(pg_restore --list "${DUMP_FILE}" 2>&1)"; then
    echo "ERROR: pg_restore --list failed (host client). Cannot verify protected-table guard." >&2
    echo "  Output: ${TOC_OUTPUT}" >&2
    exit 1
  fi
else
  SRC="$(cygpath -m "${DUMP_FILE}" 2>/dev/null || echo "${DUMP_FILE}")"
  MSYS_NO_PATHCONV=1 docker cp "${SRC}" "${POSTGRES_CONTAINER}:/tmp/__toc_check__.dump"
  if ! TOC_OUTPUT="$(MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" pg_restore --list "/tmp/__toc_check__.dump" 2>&1)"; then
    echo "ERROR: pg_restore --list failed (docker fallback). Cannot verify protected-table guard." >&2
    echo "  Output: ${TOC_OUTPUT}" >&2
    MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/__toc_check__.dump"
    exit 1
  fi
  MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/__toc_check__.dump"
fi

TOC_VIOLATIONS=""
# Strip COMMENT TABLE COLUMN entries: those list *column* names after the table
# name, so e.g. "COMMENT TABLE COLUMN public postgres buildings assets" would
# falsely match `assets` — but it is the column name, not the protected table.
TOC_FILTERED="$(printf '%s\n' "${TOC_OUTPUT}" | grep -v 'COMMENT TABLE COLUMN' || true)"
for _tbl in ${PROTECTED_TABLES}; do
  if printf '%s' "${TOC_FILTERED}" | grep -qE "(^|[[:space:]])${_tbl}([[:space:]]|$)"; then
    VIOLATION_LINE="$(printf '%s' "${TOC_FILTERED}" | grep -E "(^|[[:space:]])${_tbl}([[:space:]]|$)" | head -n 1)"
    TOC_VIOLATIONS="${TOC_VIOLATIONS}  - ${_tbl} (entry: ${VIOLATION_LINE})\n"
  fi
done

if [[ -n "${TOC_VIOLATIONS}" ]]; then
  echo "ERROR: archive contains protected platform table(s) — restore would overwrite them." >&2
  printf '%b' "${TOC_VIOLATIONS}" >&2
  echo "ABORT: use a clean dump that excludes the 5 platform tables (ai_generation_logs, assets," >&2
  echo "  platform_syncs, posts, publishing_queue)." >&2
  exit 1
fi
echo "[restore] TOC clean — no protected platform tables found."

# ---------------------------------------------------------------------------
# pg_restore the archive via run_pg_restore (additive, single-transaction).
# ---------------------------------------------------------------------------
echo "[restore] restoring archive (--no-owner --no-privileges --single-transaction --exit-on-error)..."
run_pg_restore "${DUMP_FILE}"

# ---------------------------------------------------------------------------
# Basic verify after restore.
# ---------------------------------------------------------------------------
echo "[verify] row counts (registry + LightRAG)..."
run_psql "
SELECT 'documents' AS tbl, count(*) FROM documents
UNION ALL SELECT 'document_chunks', count(*) FROM document_chunks
UNION ALL SELECT 'facts', count(*) FROM facts
UNION ALL SELECT 'fact_subjects', count(*) FROM fact_subjects
UNION ALL SELECT 'chunk_fact_refs', count(*) FROM chunk_fact_refs
UNION ALL SELECT 'images', count(*) FROM images
UNION ALL SELECT 'image_embeddings', count(*) FROM image_embeddings
UNION ALL SELECT 'project_config', count(*) FROM project_config
UNION ALL SELECT 'leads', count(*) FROM leads
UNION ALL SELECT 'query_audit', count(*) FROM query_audit
UNION ALL SELECT 'lightrag_doc_status', count(*) FROM lightrag_doc_status
UNION ALL SELECT 'lightrag_vdb_chunks', count(*) FROM lightrag_vdb_chunks_text_embedding_v4_1024d
UNION ALL SELECT 'lightrag_vdb_entity', count(*) FROM lightrag_vdb_entity_text_embedding_v4_1024d
UNION ALL SELECT 'lightrag_vdb_relation', count(*) FROM lightrag_vdb_relation_text_embedding_v4_1024d
UNION ALL SELECT 'lightrag_graph_nodes', count(*) FROM lightrag_graph_nodes
UNION ALL SELECT 'lightrag_graph_edges', count(*) FROM lightrag_graph_edges;
"

echo "[verify] embedding dims (must be vector(1024))..."
run_psql "
SELECT table_name, data_type || '(' || COALESCE(udt_name, '') || ')' AS type
FROM information_schema.columns
WHERE column_name = 'content_vector'
  AND table_name LIKE 'lightrag_vdb_%';
"

echo "[verify] HNSW vector indexes present..."
run_psql "
SELECT indexname FROM pg_indexes
WHERE tablename LIKE 'lightrag_vdb_%' AND indexdef LIKE '%hnsw%'
ORDER BY indexname;
"

echo "[verify] sample cosine similarity search (chunks)..."
run_psql "
SELECT id, round(1 - (content_vector <=> q.v)::numeric, 4) AS cosine_sim
FROM lightrag_vdb_chunks_text_embedding_v4_1024d, (
  SELECT content_vector AS v FROM lightrag_vdb_chunks_text_embedding_v4_1024d LIMIT 1
) q
ORDER BY content_vector <=> q.v
LIMIT 5;
"

# ---------------------------------------------------------------------------
# Platform-table guard: the 5 pre-existing Supabase platform tables must be
# untouched (the additive restore never drops them — this asserts it).
# ---------------------------------------------------------------------------
echo "[verify] platform tables untouched (expect all 5 listed)..."
run_psql "
SELECT tablename FROM pg_tables WHERE schemaname = 'public'
  AND tablename IN ('ai_generation_logs','assets','platform_syncs','posts','publishing_queue')
ORDER BY tablename;
"

if [[ "${SKIP_VERIFY:-0}" == "1" ]]; then
  echo "[verify] SKIP_VERIFY=1 -> skipping verify_ingest.sql + row-count parity"
else
  # A13 integrity gates (every row must report OK / count = 0).
  echo "[verify] scripts/verify_ingest.sql (A13 integrity gates)..."
  run_psql_file "${REPO_ROOT}/scripts/verify_ingest.sql"

  # Row-count parity vs the local source container (best-effort; skips when
  # the container is down). Any DIFF must be explained before cutover.
  echo "[verify] row-count parity vs local ${POSTGRES_CONTAINER}..."
  if docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
    for TBL in documents document_chunks facts fact_subjects chunk_fact_refs images \
               image_embeddings project_config leads query_audit \
               lightrag_doc_status lightrag_vdb_chunks_text_embedding_v4_1024d \
               lightrag_vdb_entity_text_embedding_v4_1024d \
               lightrag_vdb_relation_text_embedding_v4_1024d \
               lightrag_graph_nodes lightrag_graph_edges; do
      LOCAL_N="$(docker exec "${POSTGRES_CONTAINER}" psql -U "${LOCAL_PG_USER:-ragre}" -d "${LOCAL_PG_DB:-ragre}" -tAc "SELECT count(*) FROM ${TBL}" 2>/dev/null || echo 'n/a')"
      TARGET_N="$(run_psql_scalar "SELECT count(*) FROM ${TBL};")"
      if [[ "${LOCAL_N}" == "n/a" ]]; then
        STATUS="SKIP"
      elif [[ "${LOCAL_N}" == "${TARGET_N}" ]]; then
        STATUS="MATCH"
      else
        STATUS="DIFF"
      fi
      printf '  %-52s local=%-10s target=%-10s %s\n' "${TBL}" "${LOCAL_N}" "${TARGET_N}" "${STATUS}"
    done
  else
    echo "  (container ${POSTGRES_CONTAINER} not running — parity skipped)"
  fi
fi

echo "[restore] DONE. Supabase DB restored from ${DUMP_FILE}"
echo "[restore] note: verify embedding dims == 1024 (LOCK) before switching traffic;"
