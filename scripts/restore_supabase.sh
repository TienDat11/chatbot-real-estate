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
#   scripts/restore_supabase.sh <dump_file>
#
# Env:
#   SUPABASE_DB_URL   postgresql://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
#                     (direct connection; read from .env or environment)
#   POSTGRES_CONTAINER (default ragre-postgres; docker fallback client only)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DUMP_FILE="${1:-}"
if [[ -z "${DUMP_FILE}" ]] || [[ ! -f "${DUMP_FILE}" ]]; then
  echo "ERROR: usage: scripts/restore_supabase.sh <dump_file>" >&2
  exit 1
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
  echo "  Format: postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres" >&2
  exit 1
fi

# Parse the URL into components so the password never appears on a command line
# or in `ps` output. Only user:pass@host:port/db form is supported.
if [[ "${SUPABASE_DB_URL}" =~ ^postgres(ql)?://([^:]+):([^@]*)@([^:/]+)(:([0-9]+))?/([^?]+) ]]; then
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
  echo "ERROR: port 6543 is the Supavisor POOLER. Restores require the DIRECT" >&2
  echo "  connection on port 5432: postgresql://postgres:...@db.<ref>.supabase.co:5432/postgres" >&2
  exit 1
fi

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ragre-postgres}"
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
  printf 'PGPASSWORD=%s\n' "${SUPABASE_PASSWORD}" > "${PGPASS_FILE}"
  if command -v cygpath >/dev/null 2>&1; then
    PGPASS_FILE_DOCKER="$(cygpath -w "${PGPASS_FILE}")"
  else
    PGPASS_FILE_DOCKER="${PGPASS_FILE}"
  fi
fi

run_psql() {
  # $1 = SQL text; executes against Supabase via the chosen client.
  if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
    PGPASSWORD="${SUPABASE_PASSWORD}" psql \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -c "$1"
  else
    docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
      psql \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -c "$1"
  fi
}

# ---------------------------------------------------------------------------
# Confirm before overwriting anything on the target.
# ---------------------------------------------------------------------------
echo "TARGET : postgresql://${SUPABASE_USER}@${SUPABASE_HOST}:${SUPABASE_PORT}/${SUPABASE_DATABASE}"
echo "DUMP   : ${DUMP_FILE}"
read -r -p "This will overwrite the Supabase database. Type 'RESTORE' to continue: " CONFIRM
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
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
"

# ---------------------------------------------------------------------------
# pg_restore the archive. --exit-on-error: stop at the first failing object so
# a broken restore is detected immediately instead of half-applying.
# ---------------------------------------------------------------------------
echo "[restore] restoring archive (--no-owner --no-privileges --exit-on-error)..."
if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
  PGPASSWORD="${SUPABASE_PASSWORD}" pg_restore \
    -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
    -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
    --no-owner --no-privileges --exit-on-error \
    -Fc "${DUMP_FILE}"
else
  # Custom format needs random access: cp the archive into the container,
  # restore from there, then remove it. Never pipe -Fc via stdin.
  # MSYS_NO_PATHCONV stops Git Bash translating /tmp/... to a Windows path.
  docker cp "${DUMP_FILE}" "${POSTGRES_CONTAINER}:/tmp/${DUMP_BASENAME}"
  if MSYS_NO_PATHCONV=1 docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
      pg_restore \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      --no-owner --no-privileges --exit-on-error \
      -Fc "/tmp/${DUMP_BASENAME}"; then
    MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/${DUMP_BASENAME}"
  else
    MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/${DUMP_BASENAME}"
    exit 1
  fi
fi

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

echo "[restore] DONE. Supabase DB restored from ${DUMP_FILE}"
echo "[restore] note: verify embedding dims == 1024 (LOCK) and compare row counts"
echo "          against scripts/verify_ingest.sql expectations before switching traffic."
