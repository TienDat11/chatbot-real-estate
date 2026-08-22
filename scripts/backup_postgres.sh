#!/usr/bin/env bash
# =============================================================================
# backup_postgres.sh — pg_dump the local ragre database to db/backups/.
#
# WHY:
#   - Custom-format (-Fc) dump is the only format pg_restore can filter
#     selectively; it also compresses, which matters for the vector tables.
#   - --no-owner --no-privileges is required by the Supabase target: Supabase
#     manages its own roles, so ownership/GRANT statements must never be
#     emitted. The same flags keep the local restore role-agnostic too.
#   - pg_dump --version must be >= the server version. The host has no PG
#     client on PATH, so by default the dump runs inside the ragre-postgres
#     container (pg_dump 16.14 == server 16.14). Override with PG_DUMP_BIN
#     when a matching client exists on the host.
#   - The password travels only via PGPASSWORD (never on the command line).
#
# Usage:
#   scripts/backup_postgres.sh
#
# Env (read from .env if present, else the environment):
#   POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER / POSTGRES_DATABASE
#   POSTGRES_PASSWORD  (never echoed)
#   POSTGRES_CONTAINER (default ragre-postgres; used only for the docker path)
#   PG_DUMP_BIN        (default: auto-detect host pg_dump, else docker exec)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load the POSTGRES_* keys from .env without sourcing it: the file is a flat
# KEY=value list whose values may contain spaces/quotes, so `source` would try
# to execute them. Extraction is scoped to the exact keys we need and strips
# surrounding quotes. Already-exported environment vars stay authoritative.
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

POSTGRES_HOST="${POSTGRES_HOST:-$(env_get POSTGRES_HOST)}"
POSTGRES_PORT="${POSTGRES_PORT:-$(env_get POSTGRES_PORT)}"
POSTGRES_USER="${POSTGRES_USER:-$(env_get POSTGRES_USER)}"
POSTGRES_DATABASE="${POSTGRES_DATABASE:-$(env_get POSTGRES_DATABASE)}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ragre-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(env_get POSTGRES_PASSWORD)}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-ragre}"
POSTGRES_DATABASE="${POSTGRES_DATABASE:-ragre}"
export PGPASSWORD="${POSTGRES_PASSWORD}"

if [[ -z "${PGPASSWORD}" ]]; then
  echo "ERROR: POSTGRES_PASSWORD is empty — refusing to dump without a password." >&2
  exit 1
fi

BACKUP_DIR="${REPO_ROOT}/db/backups"
mkdir -p "${BACKUP_DIR}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DUMP_FILE="${BACKUP_DIR}/backup_${TIMESTAMP}.dump"

# Pick a pg_dump: host binary first (must match server version), else docker.
PG_DUMP_BIN="${PG_DUMP_BIN:-}"
if [[ -z "${PG_DUMP_BIN}" ]] && command -v pg_dump >/dev/null 2>&1; then
  PG_DUMP_BIN="pg_dump"
fi

if [[ -n "${PG_DUMP_BIN}" ]]; then
  # Host client: connect over TCP to the exposed port.
  echo "[backup] using host pg_dump (${PG_DUMP_BIN})"
  "${PG_DUMP_BIN}" \
    -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DATABASE}" \
    -Fc --no-owner --no-privileges \
    > "${DUMP_FILE}"
elif docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  # Container path: the container IS the server, so connect via localhost:5432
  # inside it and stream the custom-format dump to stdout -> host file.
  #
  # Secret handling: `docker exec -e PGPASSWORD=...` puts the value in the
  # docker client's argv, visible in `ps` on the host. An --env-file is read
  # by the docker client from disk and injected into the container process
  # environment only, so the password never appears on a command line. The
  # temp file is created 0600 and removed on exit.
  echo "[backup] using docker exec pg_dump in ${POSTGRES_CONTAINER}"
  PGPASS_FILE="$(mktemp)"
  chmod 600 "${PGPASS_FILE}"
  trap 'rm -f "${PGPASS_FILE}"' EXIT
  printf 'PGPASSWORD=%s\n' "${PGPASSWORD}" > "${PGPASS_FILE}"
  # Native docker.exe cannot read a Git Bash POSIX path; translate it via
  # cygpath so the env-file resolves identically under Git Bash, WSL, and
  # plain Linux (where cygpath is absent and the path is used as-is).
  if command -v cygpath >/dev/null 2>&1; then
    PGPASS_FILE_DOCKER="$(cygpath -w "${PGPASS_FILE}")"
  else
    PGPASS_FILE_DOCKER="${PGPASS_FILE}"
  fi
  docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
    pg_dump \
    -h localhost -p 5432 \
    -U "${POSTGRES_USER}" -d "${POSTGRES_DATABASE}" \
    -Fc --no-owner --no-privileges \
    > "${DUMP_FILE}"
else
  echo "ERROR: no pg_dump on PATH and container '${POSTGRES_CONTAINER}' is not running." >&2
  exit 1
fi

# Sanity-check the archive before declaring success: pg_restore -l lists the
# table of contents; a truncated/broken archive makes pg_restore exit non-zero.
PG_RESTORE_BIN="${PG_RESTORE_BIN:-}"
if [[ -z "${PG_RESTORE_BIN}" ]] && command -v pg_restore >/dev/null 2>&1; then
  PG_RESTORE_BIN="pg_restore"
fi
if [[ -n "${PG_RESTORE_BIN}" ]]; then
  "${PG_RESTORE_BIN}" -l "${DUMP_FILE}" >/dev/null
elif docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  # Copy the archive in and list it from a real file path: custom format needs
  # a seekable file, so piping /dev/stdin is unreliable.
  DUMP_BASENAME="$(basename "${DUMP_FILE}")"
  docker cp "${DUMP_FILE}" "${POSTGRES_CONTAINER}:/tmp/${DUMP_BASENAME}"
  # MSYS_NO_PATHCONV stops Git Bash translating /tmp/... to a Windows path.
  MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" pg_restore -l "/tmp/${DUMP_BASENAME}" >/dev/null
  MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/${DUMP_BASENAME}"
fi

SIZE="$(du -h "${DUMP_FILE}" | cut -f1)"
echo "[backup] OK: ${DUMP_FILE} (${SIZE})"
echo "[backup] host=${POSTGRES_HOST}:${POSTGRES_PORT} db=${POSTGRES_DATABASE} user=${POSTGRES_USER}"
