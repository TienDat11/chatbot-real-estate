#!/usr/bin/env bash
# Price/policy batch update (plan §3.6 dual-pace, §10 day 9) — run off-box.
# One transaction: expire old-campaign facts -> upsert price doc -> new campaign -> insert CSV facts.
# Zero re-embed: the vector index is untouched (facts live outside the index, §3.1).
# Usage: ./scripts/update_price.sh --old-campaign <k> --new-campaign <k> --project <p> --source-doc <d> \
#        --effective-from <date> --csv <file>
# CSV columns: subject_key,fact_key,policy_key,value_num,unit (AD-14). Env: POSTGRES_* from .env.
set -euo pipefail

# Load .env for POSTGRES_* credentials.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi
: "${POSTGRES_HOST:=localhost}" "${POSTGRES_PORT:=5432}" "${POSTGRES_USER:=ragre}"
: "${POSTGRES_PASSWORD:=}" "${POSTGRES_DATABASE:=ragre}"

# CLI arguments.
OLD_CAMPAIGN=""
NEW_CAMPAIGN=""
PROJECT=""
SOURCE_DOC=""
EFFECTIVE_FROM="$(date +%F)"
CSV_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --old-campaign)   OLD_CAMPAIGN="$2";   shift 2 ;;
    --new-campaign)   NEW_CAMPAIGN="$2";   shift 2 ;;
    --project)        PROJECT="$2";        shift 2 ;;
    --source-doc)     SOURCE_DOC="$2";     shift 2 ;;
    --effective-from) EFFECTIVE_FROM="$2"; shift 2 ;;
    --csv)            CSV_FILE="$2";       shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "${OLD_CAMPAIGN}" || -z "${NEW_CAMPAIGN}" || -z "${PROJECT}" \
      || -z "${SOURCE_DOC}" || -z "${CSV_FILE}" ]]; then
  echo "thiếu tham số bắt buộc (--old-campaign --new-campaign --project --source-doc --csv)" >&2
  exit 2
fi
if [[ ! -f "${CSV_FILE}" ]]; then
  echo "CSV không tồn tại: ${CSV_FILE}" >&2
  exit 2
fi

BACKUP_DIR="${REPO_ROOT}/backups"
mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="${BACKUP_DIR}/price_${NEW_CAMPAIGN}_${STAMP}.dump"

# 1. Backup the affected tables.
echo "==> backup pg_dump → ${DUMP}"
PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
  -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DATABASE}" \
  -t documents -t campaigns -t fact_subjects -t facts -t chunk_fact_refs \
  -F c -f "${DUMP}"
echo "    backup OK ($(du -h "${DUMP}" | cut -f1))"

# 2. Expire old facts and insert the new batch in one transaction (inline Python helper).
echo "==> expire facts cũ [${OLD_CAMPAIGN}] + insert đợt mới [${NEW_CAMPAIGN}]"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
# Strip a UTF-8 BOM from the CSV before handing it to Python.
sed -e 's/^\xEF\xBB\xBF//' "${CSV_FILE}" > "${WORKDIR}/new_price.csv"

PYTHON_BIN="${PYTHON_BIN:-python}"
"${PYTHON_BIN}" - "${OLD_CAMPAIGN}" "${NEW_CAMPAIGN}" "${PROJECT}" "${SOURCE_DOC}" "${EFFECTIVE_FROM}" "${WORKDIR}/new_price.csv" <<'PYEOF'
"""Inline helper: expire old facts and insert a new batch atomically (asyncpg, §3.6).

Any error rolls everything back — the batch never lands half-applied.
"""
import asyncio
import csv
import hashlib
import os
import sys
from decimal import Decimal

import asyncpg


async def main(old_campaign, new_campaign, project, source_doc, effective_from, csv_path) -> None:
    conn = await asyncpg.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "ragre"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        database=os.getenv("POSTGRES_DATABASE", "ragre"),
    )
    try:
        async with conn.transaction():
            # 2a. Close open facts for the old campaign (half-open [old_from, effective_to)).
            #     Default (--effective-from = today) yields effective_to = CURRENT_DATE (§3.6);
            #     LEAST() guards against interval overlap with the new facts (facts_no_overlap).
            old = await conn.fetchval(
                "UPDATE facts SET effective_to = LEAST($2::date, CURRENT_DATE) "
                "WHERE campaign_key = $1 AND effective_to IS NULL",
                old_campaign, effective_from,
            )
            print(f"    expired facts: {old}")

            # 2b. Upsert the new price document (published so RLS permits fact SELECTs).
            content_hash = hashlib.sha256(f"seed:{source_doc}".encode()).hexdigest()
            await conn.execute(
                """
                INSERT INTO documents (doc_id, kind, title, source_file, effective_from, status, content_hash, metadata)
                VALUES ($1, 'price', $2, $3, $4::date, 'published', $5,
                        jsonb_build_object('project', $6, 'campaign', $2, 'currency', 'VND'))
                ON CONFLICT (doc_id) DO UPDATE
                  SET effective_from = EXCLUDED.effective_from, updated_at = now()
                """,
                source_doc, f"Bảng giá {project} đợt mới", f"{source_doc}.csv",
                effective_from, content_hash, project,
            )

            # 2c. Upsert the new campaign (idempotent).
            await conn.execute(
                """
                INSERT INTO campaigns (campaign_key, project_key, effective_from, source_doc_id, status)
                VALUES ($1, $2, $3::date, $4, 'active')
                ON CONFLICT (campaign_key) DO UPDATE
                  SET effective_from = EXCLUDED.effective_from, status = 'active'
                """,
                new_campaign, project, effective_from, source_doc,
            )

            # 2d. Insert new facts from the CSV, keeping the CSV's numeric types.
            inserted = 0
            with open(csv_path, encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    subject_key = row["subject_key"].strip()
                    fact_key = row["fact_key"].strip()
                    policy_key = (row["policy_key"].strip() or None)
                    value_num = Decimal(row["value_num"].strip())
                    unit = row["unit"].strip()
                    if unit not in ("vnd", "m2", "pct", "months", "days", "enum"):
                        raise ValueError(f"unit không hợp lệ: {unit} (row {subject_key}/{fact_key})")
                    sid = await conn.fetchval(
                        "SELECT id FROM fact_subjects WHERE subject_key = $1", subject_key
                    )
                    if sid is None:
                        raise ValueError(f"subject_key chưa tồn tại trong fact_subjects: {subject_key}")
                    await conn.execute(
                        """
                        INSERT INTO facts
                          (subject_id, fact_key, policy_key, campaign_key, value_num, unit,
                           quality, volatile, effective_from, effective_to, source_doc_id, extract_conf)
                        VALUES ($1,$2,$3,$4,$5,$6,'exact',false,$7::date,NULL,$8,0.99)
                        ON CONFLICT DO NOTHING
                        """,
                        sid, fact_key, policy_key, new_campaign, value_num, unit,
                        effective_from, source_doc,
                    )
                    inserted += 1
            print(f"    inserted facts: {inserted}")
        # The transaction commits here (async with).
        print("    COMMIT OK — answer sẽ đổi ngay sau transaction này")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
PYEOF

# 3. Run a golden-set regression.
echo "==> golden-set regression (eval --subset 20 — gồm legal + affordability + aggregate)"
"${PYTHON_BIN}" eval/run_eval.py --subset 20 --fail-fast || {
  echo "    ❌ regression fail — xem eval log; rollback: pg_restore -F c ${DUMP}" >&2
  exit 1
}

# 4. Note: zero re-embed.
echo "============================================================"
echo "DONE. 0 re-embed: vector index KHÔNG bị chạm (facts ngoài vector §3.1)."
echo "Optionally verify: psql -f scripts/verify_ingest.sql"
echo "============================================================"