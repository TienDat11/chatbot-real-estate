#!/usr/bin/env bash
# =============================================================================
# sync_supabase.sh — REUSABLE "local -> Supabase" data refresh (app tables).
#
# WHY:
#   The local docker database (ragre-postgres) is the authoring source (ingest,
#   re-ingest, migrations). This script copies ITS DATA into Supabase staging so
#   the app runtime (now pointed at the Supabase pooler) serves fresh data.
#   It is the repeatable companion of the one-shot restore_supabase.sh:
#     - restore_supabase.sh : full schema+data bootstrap (first deploy only)
#     - sync_supabase.sh    : data-only refresh, repeatable, self-gating
#
# WHAT IT DOES:
#   1. pg_dump --data-only --no-owner --no-privileges from the LOCAL container
#      (container-local socket, like the seed dump) for OUR app tables ONLY —
#      the 51-table list below is transcribed from the seed dump TOC
#      (db/backups/supabase_seed_20260906_015326.toc.txt, lines 136-186).
#      The 5 Supabase platform tables (ai_generation_logs, assets,
#      platform_syncs, posts, publishing_queue) are NOT local and MUST NEVER
#      enter this pipeline — guarded below in three places.
#   2. On Supabase, in ONE transaction (psql --single-transaction):
#        SET statement_timeout=0; SET lock_timeout='60s';
#        TRUNCATE TABLE <all 51 in ONE statement> RESTART IDENTITY;
#        <pg_restore --data-only SQL of the fresh dump>
#      One TRUNCATE statement covering the whole table group is FK-safe
#      without CASCADE because every table referencing another listed table is
#      itself listed (verified against the FK graph; guard re-checks that no FK
#      bridges the app set and the platform set). RESTART IDENTITY resets
#      sequences; the absolute setvals carried in the data dump then restore
#      the correct counters (verified after dump: SEQUENCE SET items > 0).
#      --exit-on-error + ON_ERROR_STOP + --single-transaction => any failure
#      rolls back to the pre-sync Supabase state.
#   3. Post-verify (read-only): 16-table row-count parity local vs Supabase
#      (DIFF must be 0 except the two live-mutable tables leads/query_audit,
#      which are WARN-only because the local dev DB keeps moving after the
#      dump snapshot), platform tables untouched, qd6608 doc version report,
#      vdb_entity workspace counts.
#
# NOTES:
#   - The 16-table parity list uses the ACTIVE embedding tables
#     (gemini_embedding_001_* per EMBEDDING_MODEL=gemini-embedding-001); the
#     restore script's list named the legacy text_embedding_v4 trio.
#   - TRUNCATE takes ACCESS EXCLUSIVE locks; stop any server currently writing
#     to Supabase before running (lock_timeout 60s fails fast instead of hanging).
#   - Credentials: SUPABASE_DB_URL is parsed from .env in-process (split at the
#     LAST '@' — passwords may contain '@'; libpq and the restore script agree).
#     The password travels only via a 0600 env-file to docker exec / env var to
#     the host psql — NEVER on an argv line, NEVER echoed.
#
# Usage:
#   scripts/sync_supabase.sh [--dry-run]
#
# Env:
#   SUPABASE_DB_URL    Supavisor pooler SESSION URI (allowlisted host only):
#                      postgresql://postgres.<ref>:<pw>@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres
#   POSTGRES_CONTAINER (default ragre-postgres) — local source container
#   LOCAL_PG_USER / LOCAL_PG_DB (defaults ragre/ragre) — local dump identity
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

DRY_RUN=0
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,60p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "ERROR: unknown argument '${arg}' (use --dry-run or -h)" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# OUR app tables — transcribed from db/backups/supabase_seed_20260906_015326.toc.txt
# (TABLE DATA entries, lines 136-186). Sorted; 51 tables.
# ---------------------------------------------------------------------------
OUR_TABLES=(
  anon_quota campaigns chat_messages chat_sessions chunk_fact_refs
  document_chunks documents fact_aliases fact_subjects facts
  fcm_device_tokens floor_labels image_embeddings images ingest_log
  ip_rate_limit leads lightrag_doc_chunks lightrag_doc_full lightrag_doc_status
  lightrag_entity_chunks lightrag_full_entities lightrag_full_relations
  lightrag_graph_edges lightrag_graph_nodes lightrag_llm_cache
  lightrag_relation_chunks
  lightrag_vdb_chunks_gemini_embedding_001_1024d
  lightrag_vdb_chunks_gemini_embedding_2_1024d
  lightrag_vdb_chunks_openai_text_embedding_3_small_1024d
  lightrag_vdb_chunks_text_embedding_v4_1024d
  lightrag_vdb_chunks_voyageai_rerank_2_5_1024d
  lightrag_vdb_entity_gemini_embedding_001_1024d
  lightrag_vdb_entity_gemini_embedding_2_1024d
  lightrag_vdb_entity_openai_text_embedding_3_small_1024d
  lightrag_vdb_entity_text_embedding_v4_1024d
  lightrag_vdb_entity_voyageai_rerank_2_5_1024d
  lightrag_vdb_relation_gemini_embedding_001_1024d
  lightrag_vdb_relation_gemini_embedding_2_1024d
  lightrag_vdb_relation_openai_text_embedding_3_small_1024d
  lightrag_vdb_relation_text_embedding_v4_1024d
  lightrag_vdb_relation_voyageai_rerank_2_5_1024d
  project_config query_audit quota_grant_audit quota_reservations review_queue
  sales sales_assignment_log sales_notification_reads staff_audit_log
)

PLATFORM_TABLES=(ai_generation_logs assets platform_syncs posts publishing_queue)

# 16-table parity set (ACTIVE embedding trio — see header note).
PARITY_TABLES=(
  documents document_chunks facts fact_subjects chunk_fact_refs images
  image_embeddings project_config leads query_audit lightrag_doc_status
  lightrag_vdb_chunks_gemini_embedding_001_1024d
  lightrag_vdb_entity_gemini_embedding_001_1024d
  lightrag_vdb_relation_gemini_embedding_001_1024d
  lightrag_graph_nodes lightrag_graph_edges
)
# Live-mutable local tables: a DIFF vs the dump snapshot is WARN-only.
MUTABLE_OK=(leads query_audit)

# GUARD 1: the table list must never contain a platform table.
for pt in "${PLATFORM_TABLES[@]}"; do
  for t in "${OUR_TABLES[@]}"; do
    if [[ "${t}" == "${pt}" ]]; then
      echo "ABORT: platform table '${pt}' found in OUR_TABLES — never sync it." >&2
      exit 1
    fi
  done
done

ALLOWED_HOST="aws-1-ap-southeast-1.pooler.supabase.com"

# ---------------------------------------------------------------------------
# Credentials — load SUPABASE_DB_URL from .env without sourcing it.
# ---------------------------------------------------------------------------
ENV_FILE="${REPO_ROOT}/.env"
env_get() {
  local key="$1" line
  if [[ -f "${ENV_FILE}" ]]; then
    line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
    line="${line#*=}"
    line="${line%\"*}"; line="${line#\"*}"
    line="${line%%\"*}"; line="${line#\"*}"
    line="${line%%\#*}"
    printf '%s' "${line}"
  fi
}

SUPABASE_DB_URL="${SUPABASE_DB_URL:-$(env_get SUPABASE_DB_URL)}"
if [[ -z "${SUPABASE_DB_URL}" ]]; then
  echo "ERROR: SUPABASE_DB_URL is not set (put it in .env or export it)." >&2
  exit 1
fi

# Split at the LAST '@' (passwords may contain '@').
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

# GUARD 2: only the allowlisted pooler session endpoint may be refreshed.
if [[ "${SUPABASE_HOST}" != "${ALLOWED_HOST}" ]]; then
  echo "ABORT: refusing to sync against '${SUPABASE_HOST}' — only ${ALLOWED_HOST} is allowlisted." >&2
  exit 1
fi
if [[ "${SUPABASE_PORT}" == "6543" ]]; then
  echo "ABORT: port 6543 is the transaction-mode pooler — a single-transaction sync needs session mode :5432." >&2
  exit 1
fi
if [[ "${SUPABASE_PORT}" != "5432" ]]; then
  echo "ABORT: unexpected port '${SUPABASE_PORT}' (expected 5432 session mode)." >&2
  exit 1
fi

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-ragre-postgres}"

# SQL literal lists for the preflight queries.
SQL_OUR_LIST="$(printf "'%s'," "${OUR_TABLES[@]}")"; SQL_OUR_LIST="${SQL_OUR_LIST%,}"
SQL_QUAL_OUR_LIST="$(printf "'public.%s'," "${OUR_TABLES[@]}")"; SQL_QUAL_OUR_LIST="${SQL_QUAL_OUR_LIST%,}"
SQL_PLAT_LIST="$(printf "'%s'," "${PLATFORM_TABLES[@]}")"; SQL_PLAT_LIST="${SQL_PLAT_LIST%,}"
SQL_QUAL_PLAT_LIST="$(printf "'public.%s'," "${PLATFORM_TABLES[@]}")"; SQL_QUAL_PLAT_LIST="${SQL_QUAL_PLAT_LIST%,}"
TRUNCATE_LIST="$(printf "public.%s, " "${OUR_TABLES[@]}")"; TRUNCATE_LIST="${TRUNCATE_LIST%, }"

# ---------------------------------------------------------------------------
# Client selection (same policy as restore_supabase.sh): host psql/pg_dump/
# pg_restore if present, else the local postgres container for everything.
# The container talks to Supabase over the pooler with an env-file; the
# password never appears on a command line.
# ---------------------------------------------------------------------------
HAVE_HOST_CLIENT=0
if command -v pg_restore >/dev/null 2>&1 && command -v psql >/dev/null 2>&1 && command -v pg_dump >/dev/null 2>&1; then
  HAVE_HOST_CLIENT=1
elif ! docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  echo "ERROR: no host psql/pg_restore/pg_dump AND container '${POSTGRES_CONTAINER}' is not running." >&2
  exit 1
fi

# Credential env-file (docker path): created 0600, removed on exit.
PGPASS_FILE=""
PGPASS_FILE_DOCKER=""
if [[ "${HAVE_HOST_CLIENT}" != "1" ]]; then
  PGPASS_FILE="$(mktemp)"
  chmod 600 "${PGPASS_FILE}"
  trap 'rm -f "${PGPASS_FILE}"' EXIT
  printf 'PGPASSWORD=%s\nPGSSLMODE=%s\n' "${SUPABASE_PASSWORD}" "require" > "${PGPASS_FILE}"
  if command -v cygpath >/dev/null 2>&1; then
    PGPASS_FILE_DOCKER="$(cygpath -w "${PGPASS_FILE}")"
  else
    PGPASS_FILE_DOCKER="${PGPASS_FILE}"
  fi
fi

run_psql() {
  # $1 = SQL text (may contain newlines). Read-only or post-verify usage.
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
  # $1 = SQL file on the HOST.
  local SRC
  SRC="$(cygpath -m "$1" 2>/dev/null || echo "$1")"
  if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
    PGPASSWORD="${SUPABASE_PASSWORD}" psql -w -tA \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -f "$1"
  else
    MSYS_NO_PATHCONV=1 docker cp "${SRC}" "${POSTGRES_CONTAINER}:/tmp/__sync_verify__.sql"
    MSYS_NO_PATHCONV=1 docker exec --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
      psql -w -tA \
      -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
      -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
      -v ON_ERROR_STOP=1 -X -q -f "/tmp/__sync_verify__.sql"
    MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f "/tmp/__sync_verify__.sql"
  fi
}

# ---------------------------------------------------------------------------
# GUARD 3 (read-only preflight, runs in --dry-run too).
# ---------------------------------------------------------------------------
echo "[preflight] local source container: ${POSTGRES_CONTAINER}"
LOCAL_COUNT="$(docker exec "${POSTGRES_CONTAINER}" psql -U "${LOCAL_PG_USER:-ragre}" -d "${LOCAL_PG_DB:-ragre}" -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename IN (${SQL_OUR_LIST})")"
if [[ "${LOCAL_COUNT}" != "${#OUR_TABLES[@]}" ]]; then
  echo "ABORT: expected ${#OUR_TABLES[@]} local app tables, found ${LOCAL_COUNT}." >&2
  docker exec "${POSTGRES_CONTAINER}" psql -U "${LOCAL_PG_USER:-ragre}" -d "${LOCAL_PG_DB:-ragre}" -tAc \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename IN (${SQL_OUR_LIST}) ORDER BY 1" >&2
  exit 1
fi
echo "[preflight] local app tables present: ${LOCAL_COUNT}/${#OUR_TABLES[@]}"

TARGET_DB="$(run_psql "SELECT current_database() || ' @ ' || inet_server_addr() || ':' || inet_server_port()")"
echo "[preflight] target(redacted): ${SUPABASE_USER}@${SUPABASE_HOST}:${SUPABASE_PORT}/${SUPABASE_DATABASE} (server: ${TARGET_DB})"

SUPER_COUNT="$(run_psql "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename IN (${SQL_OUR_LIST})")"
if [[ "${SUPER_COUNT}" != "${#OUR_TABLES[@]}" ]]; then
  echo "ABORT: target is missing app tables (found ${SUPER_COUNT}/${#OUR_TABLES[@]}) — run restore_supabase.sh first." >&2
  exit 1
fi
echo "[preflight] target app tables present: ${SUPER_COUNT}/${#OUR_TABLES[@]}"

FK_BRIDGE="$(run_psql "SELECT count(*) FROM pg_constraint c WHERE c.contype='f' AND ((c.conrelid::regclass::text IN (${SQL_QUAL_PLAT_LIST}) AND c.confrelid::regclass::text IN (${SQL_QUAL_OUR_LIST})) OR (c.conrelid::regclass::text IN (${SQL_QUAL_OUR_LIST}) AND c.confrelid::regclass::text IN (${SQL_QUAL_PLAT_LIST})))")"
if [[ "${FK_BRIDGE}" != "0" ]]; then
  echo "ABORT: ${FK_BRIDGE} FK constraint(s) bridge app tables and platform tables — TRUNCATE would not be safe." >&2
  run_psql "SELECT c.conrelid::regclass || ' -> ' || c.confrelid::regclass FROM pg_constraint c WHERE c.contype='f' AND ((c.conrelid::regclass::text IN (${SQL_QUAL_PLAT_LIST}) AND c.confrelid::regclass::text IN (${SQL_QUAL_OUR_LIST})) OR (c.conrelid::regclass::text IN (${SQL_QUAL_OUR_LIST}) AND c.confrelid::regclass::text IN (${SQL_QUAL_PLAT_LIST})))" >&2
  exit 1
fi
echo "[preflight] no FK between app tables and platform tables: OK"

echo "[preflight] pre-sync platform counts (must be unchanged after sync):"
for pt in "${PLATFORM_TABLES[@]}"; do
  echo "  ${pt}=$(run_psql "SELECT count(*) FROM public.${pt}")"
done

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
DUMP_OUT="${REPO_ROOT}/db/backups/supabase_sync_$(date +%Y%m%d_%H%M%S).dump"
TOC_OUT="${DUMP_OUT%.dump}.toc.txt"

echo "[plan] dump  : docker exec ${POSTGRES_CONTAINER} pg_dump -Fc --data-only --no-owner --no-privileges --no-comments (${#OUR_TABLES[@]} tables via -t) -> ${DUMP_OUT}"
echo "[plan] txn   : psql --single-transaction: SET statement_timeout=0; SET lock_timeout='60s';"
echo "[plan]         TRUNCATE TABLE ${TRUNCATE_LIST} RESTART IDENTITY;"
echo "[plan]         + pg_restore -f - (dump SQL) piped into the same psql session"
echo "[plan] verify: sequence-setval items > 0; 16-table parity DIFF=0 (leads/query_audit WARN-only); platform untouched"

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "[dry-run] full table list:"
  printf '  %s\n' "${OUR_TABLES[@]}"
  echo "[dry-run] preflight + plan only — ZERO writes performed."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: dump local data (container-local socket) + TOC snapshot.
# ---------------------------------------------------------------------------
TABLE_ARGS=()
for t in "${OUR_TABLES[@]}"; do TABLE_ARGS+=("-t" "public.${t}"); done

echo "[dump] pg_dump -> ${DUMP_OUT}"
docker exec "${POSTGRES_CONTAINER}" pg_dump \
  -U "${LOCAL_PG_USER:-ragre}" -d "${LOCAL_PG_DB:-ragre}" \
  -Fc --data-only --no-owner --no-privileges --no-comments \
  "${TABLE_ARGS[@]}" > "${DUMP_OUT}"
echo "[dump] wrote $(du -h "${DUMP_OUT}" | cut -f1)"

# Copy into the container once; used for both TOC verification and the restore.
MSYS_NO_PATHCONV=1 docker cp "$(cygpath -m "${DUMP_OUT}" 2>/dev/null || echo "${DUMP_OUT}")" "${POSTGRES_CONTAINER}:/tmp/sync_supabase.dump"

MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" pg_restore --list /tmp/sync_supabase.dump > "${TOC_OUT}"

# GUARD: platform tables must not appear anywhere in the archive.
if grep -Eq "(^|[^A-Za-z_])(ai_generation_logs|assets|platform_syncs|posts|publishing_queue)([^A-Za-z_]|$)" "${TOC_OUT}"; then
  echo "ABORT: dump TOC mentions a platform table — refusing to restore." >&2
  exit 1
fi

# GUARD: sequence setvals must be carried in the data dump (they follow the
# TRUNCATE ... RESTART IDENTITY inside the same transaction).
SEQ_COUNT="$(grep -c "SEQUENCE SET" "${TOC_OUT}" || true)"
if [[ "${SEQ_COUNT}" -eq 0 ]]; then
  echo "ABORT: dump carries no SEQUENCE SET items — sequences would stay at 1 after RESTART IDENTITY." >&2
  exit 1
fi
echo "[dump] TOC: $(grep -c "TABLE DATA" "${TOC_OUT}") TABLE DATA + ${SEQ_COUNT} SEQUENCE SET items (-> ${TOC_OUT})"

# ---------------------------------------------------------------------------
# Step 2: ONE transaction on Supabase — TRUNCATE all + restore fresh data.
# ---------------------------------------------------------------------------
echo "[sync] single transaction: TRUNCATE ${#OUR_TABLES[@]} tables (RESTART IDENTITY) + data restore..."
SECONDS=0
{
  printf "SET statement_timeout = 0;\nSET lock_timeout = '60s';\nTRUNCATE TABLE %s RESTART IDENTITY;\n" "${TRUNCATE_LIST}"
  MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" pg_restore -f - --exit-on-error /tmp/sync_supabase.dump
} | if [[ "${HAVE_HOST_CLIENT}" == "1" ]]; then
  PGPASSWORD="${SUPABASE_PASSWORD}" psql -w \
    -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
    -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
    --single-transaction -v ON_ERROR_STOP=1 -X -q
else
  docker exec -i --env-file "${PGPASS_FILE_DOCKER}" "${POSTGRES_CONTAINER}" \
    psql -w \
    -h "${SUPABASE_HOST}" -p "${SUPABASE_PORT}" \
    -U "${SUPABASE_USER}" -d "${SUPABASE_DATABASE}" \
    --single-transaction -v ON_ERROR_STOP=1 -X -q
fi
echo "[sync] transaction committed in ${SECONDS}s"
MSYS_NO_PATHCONV=1 docker exec "${POSTGRES_CONTAINER}" rm -f /tmp/sync_supabase.dump

# ---------------------------------------------------------------------------
# Step 3: post-verify (read-only).
# ---------------------------------------------------------------------------
echo "[verify] 16-table parity local vs Supabase (snapshot vs post-sync):"
PARITY_FAIL=0
for t in "${PARITY_TABLES[@]}"; do
  LOCAL_N="$(docker exec "${POSTGRES_CONTAINER}" psql -U "${LOCAL_PG_USER:-ragre}" -d "${LOCAL_PG_DB:-ragre}" -tAc "SELECT count(*) FROM public.${t}" 2>/dev/null || echo 'n/a')"
  TARGET_N="$(run_psql "SELECT count(*) FROM public.${t}")"
  STATUS="DIFF"
  if [[ "${LOCAL_N}" == "${TARGET_N}" ]]; then STATUS="MATCH"; fi
  if [[ "${STATUS}" == "DIFF" ]]; then
    WARN_ONLY=0
    for m in "${MUTABLE_OK[@]}"; do [[ "${t}" == "${m}" ]] && WARN_ONLY=1; done
    if [[ "${WARN_ONLY}" == "1" ]]; then STATUS="DIFF(warn: live-mutable)"; else PARITY_FAIL=1; fi
  fi
  printf '  %-52s local=%-10s supabase=%-10s %s\n' "${t}" "${LOCAL_N}" "${TARGET_N}" "${STATUS}"
done
if [[ "${PARITY_FAIL}" != "0" ]]; then
  echo "ERROR: parity DIFF on content table(s) — investigate before serving traffic." >&2
  exit 1
fi

echo "[verify] platform tables untouched:"
for pt in "${PLATFORM_TABLES[@]}"; do
  echo "  ${pt}=$(run_psql "SELECT count(*) FROM public.${pt}")"
done

echo "[verify] qd6608 doc version on Supabase:"
run_psql "SELECT '  ' || doc_id || ' | v' || version || ' | ' || title || ' | effective_from=' || effective_from FROM documents WHERE doc_id LIKE '%qd6608%'"
run_psql "SELECT '  chunks=' || count(*) FROM document_chunks WHERE doc_id LIKE '%qd6608%'"
run_psql "SELECT '  facts=' || count(*) FROM facts WHERE source_doc_id LIKE '%qd6608%'"

echo "[verify] vdb_entity workspace counts (active gemini_embedding_001 table):"
run_psql "SELECT '  ' || COALESCE(workspace,'<null>') || '=' || count(*) FROM lightrag_vdb_entity_gemini_embedding_001_1024d GROUP BY workspace ORDER BY workspace"

echo "[sync] DONE. Supabase refreshed from ${DUMP_OUT}"
