#!/usr/bin/env bash
# =============================================================================
# rag-real-estate — scripts/update_6mo.sh (update pháp lý định kỳ ~6 tháng)
# Plan §3.6 + §10 Ngày 10 + §4.8 | Chạy OFF-BOX (máy dev, KHÔNG trên VPS 4GB)
#
# Chuỗi: backup → ingest incremental (chỉ doc đổi, text_hash) → verify → regression
#        → re-baseline golden.
#
# Nguyên tắc:
#   * Pháp lý đổi ~6 tháng/lần → incremental (LightRAG merge node/edge, KHÔNG rebuild).
#   * Chỉ chunk ĐỔI (text_hash mới) được re-embed — đường dẫn vector tối thiểu.
#   * Filter hiệu lực: văn bản cũ đánh status='expired' + effective_to (§4.3) —
#     KHÔNG xóa; hydrate tự loại.
#   * Backup trước mọi thay đổi; rollback có runbook (pg_restore + re-baseline cũ).
#
# Usage:
#   ./scripts/update_6mo.sh --docs-dir data/legal_2026H2 --changed-list data/changed_doc_ids.txt
#   --docs-dir    : thư mục chứa file gốc mới/cập nhật
#   --changed-list: file 1 dòng/doc_id (doc_id đổi trong kỳ này) — ingest/load.py xử lý
#
# CRM-level notes (tích hợp sau khi có CRM):
#   * Đơn vị kinh doanh gửi danh sách văn bản mới + ngày hiệu lực (data owner).
#   * Nhập `effective_from/effective_to` trong metadata file kèm doc.
#   * Sau update: thông báo mua giới qua CRM về văn bản hết hiệu lực (vd Luật cũ).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- load .env --------------------------------------------------------------
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi
: "${POSTGRES_HOST:=localhost}" "${POSTGRES_PORT:=5432}" "${POSTGRES_USER:=ragre}"
: "${POSTGRES_PASSWORD:=}" "${POSTGRES_DATABASE:=ragre}"
PYTHON_BIN="${PYTHON_BIN:-python}"

DOCS_DIR=""
CHANGED_LIST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --docs-dir)      DOCS_DIR="$2";      shift 2 ;;
    --changed-list)  CHANGED_LIST="$2";  shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "${DOCS_DIR}" || -z "${CHANGED_LIST}" ]]; then
  echo "cần --docs-dir và --changed-list" >&2
  exit 2
fi
[[ -d "${DOCS_DIR}" ]] || { echo "docs-dir không tồn tại: ${DOCS_DIR}" >&2; exit 2; }
[[ -f "${CHANGED_LIST}" ]] || { echo "changed-list không tồn tại: ${CHANGED_LIST}" >&2; exit 2; }

BACKUP_DIR="${REPO_ROOT}/backups"
mkdir -p "${BACKUP_DIR}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="${BACKUP_DIR}/legal_${STAMP}.dump"

# --- 1. backup toàn bộ (registry + graph/vector của LightRAG) --------------
echo "==> backup full (documents, facts, chunks, refs, campaigns, LightRAG tables)"
PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
  -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DATABASE}" -F c -f "${DUMP}"
echo "    backup OK → ${DUMP}"

# --- 2. ingest incremental (off-box) ---------------------------------------
# ingest/load.py CLI: python -m ingest.load --dir <docs_dir> --changed <csv-doc_ids>
# Chỉ xử lý doc_id trong --changed; mỗi doc: parse → extract → COMMIT registry
# (documents/chunks/facts/refs) → LightRAG ainsert chunk placeholder-annotated.
echo "==> ingest incremental: ${DOCS_DIR} (${CHANGED_LIST})"
if [[ -f ingest/load.py ]]; then
  CHANGED_CSV="$(paste -sd, "${CHANGED_LIST}")"
  "${PYTHON_BIN}" -m ingest.load --dir "${DOCS_DIR}" --changed "${CHANGED_CSV}"
else
  echo "    ❌ ingest/load.py chưa tồn tại — dừng (fail-loud, không chạy mù)." >&2
  exit 1
fi

# --- 3. verify integrity (A13) ----------------------------------------------
echo "==> verify_ingest.sql — mọi count phải = 0"
PGPASSWORD="${POSTGRES_PASSWORD}" psql \
  -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" \
  -d "${POSTGRES_DATABASE}" -f scripts/verify_ingest.sql
# (đọc kết quả thủ công — mọi check_name phải OK)

# --- 4. regression full golden set -----------------------------------------
echo "==> golden-set regression (full)"
"${PYTHON_BIN}" eval/run_eval.py --fail-fast --json-out "backups/regression_${STAMP}.json"

# --- 5. re-baseline ---------------------------------------------------------
# So sánh với baseline cũ (backups/baseline.json nếu có); delta ngưỡng ~0.05 (§11).
# PASS → ghi baseline mới.
REGRESSION_JSON="backups/regression_${STAMP}.json"
BASELINE="backups/baseline.json"
if [[ -f "${BASELINE}" ]]; then
  echo "==> so sánh baseline cũ (delta threshold ~0.05)"
  "${PYTHON_BIN}" - "${BASELINE}" "${REGRESSION_JSON}" <<'PYEOF'
import json, sys
old = json.loads(open(sys.argv[1], encoding="utf-8").read())
new = json.loads(open(sys.argv[2], encoding="utf-8").read())
gate_ok = True
for k in ("numeric", "faithfulness", "overall_pass"):
    a = old.get("rates", {}).get(k, 0)
    b = new.get("rates", {}).get(k, 0)
    d = b - a
    print(f"  {k}: old={a:.3f} new={b:.3f} delta={d:+.3f} {'OK' if d >= -0.05 else 'CHECK'}")
    if d < -0.05:
        gate_ok = False
if not gate_ok:
    print("  ⚠️ tụt > 0.05 — xem lại ingest trước khi re-baseline", file=sys.stderr)
    sys.exit(1)
PYEOF
else
  echo "==> chưa có baseline cũ — tạo baseline đầu tiên"
fi
cp "${REGRESSION_JSON}" "${BASELINE}"
echo "  baseline mới: ${BASELINE}"

echo "============================================================"
echo "DONE. Update pháp lý 6 tháng hoàn tất."
echo "  - backup:      ${DUMP}"
echo "  - regression:  backups/regression_${STAMP}.json"
echo "  - baseline:    backups/baseline.json (mới)"
echo "  - KHÔNG rebuild: LightRAG incremental merge node/edge (§3.6)."
echo "  - Golden set: đã chạy full theo §11; re-baseline xong."
echo "  - CRM: gửi thông báo văn bản hết hiệu lực cho mua giới (nếu có)."
echo "============================================================"