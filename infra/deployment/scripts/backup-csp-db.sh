#!/usr/bin/env bash
# ============================================================================
# backup-csp-db.sh — CSP Postgres 定時備份（P3.1）
# ----------------------------------------------------------------------------
# 一人維運、內網 air-gapped：cron + 本腳本即可，不另起 daemon、不上雲。
#
# 產出：可還原的 custom-format 壓縮檔（pg_dump -Fc），副檔名 .dump。
# 檔名只含時間戳，不含密碼／主機祕密（PUBLIC repo 友好）。
#
# 用法（在平台主機）：
#   ANILA_DB_CONTAINER=anila-csp-db-1 \
#   bash infra/deployment/scripts/backup-csp-db.sh
#
# 預設寫到家目錄底下（~/anila-backups），所以不必 sudo 就跑得動。
# 備份最怕的是「因為麻煩所以沒跑」；要放系統目錄請帶 ANILA_BACKUP_DIR=。
#
# 還原手順見 docs/runbooks/csp-db-backup-restore.md
#
# 環境變數（皆可選，有合理預設）：
#   ANILA_DB_CONTAINER   pg 容器名（預設：偵測 *csp-db* 且 Up 的第一個）
#   ANILA_DB_NAME        資料庫名（預設 csp）
#   ANILA_DB_USER        超級使用者（預設 csp；容器內 peer/trust，不需密碼）
#   ANILA_BACKUP_DIR     備份目錄（預設 ~/anila-backups，不需 sudo）
#   ANILA_BACKUP_KEEP_DAYS   每日檔保留天數（預設 30）
#   ANILA_BACKUP_KEEP_MONTHLY 月初檔保留個月數（預設 7，約半年稽核窗）
# ============================================================================
set -euo pipefail

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

# 絕不把密碼印到 stdout／檔名。容器內以 -U 連本機 socket，不帶 PGPASSWORD。
DB_NAME="${ANILA_DB_NAME:-csp}"
DB_USER="${ANILA_DB_USER:-csp}"
BACKUP_DIR="${ANILA_BACKUP_DIR:-${HOME:-/root}/anila-backups}"
KEEP_DAYS="${ANILA_BACKUP_KEEP_DAYS:-30}"
KEEP_MONTHLY="${ANILA_BACKUP_KEEP_MONTHLY:-7}"

resolve_container() {
  if [[ -n "${ANILA_DB_CONTAINER:-}" ]]; then
    printf '%s\n' "$ANILA_DB_CONTAINER"
    return
  fi
  # 常見：anila-csp-db-1（project anila）
  local matches match count
  matches="$(docker ps --format '{{.Names}}' | grep -E 'csp-db' || true)"
  if [[ -z "$matches" ]]; then
    fail "找不到執行中的 csp-db 容器；請設 ANILA_DB_CONTAINER"
  fi
  count=$(printf "%s\n" "$matches" | grep -c .)
  if [[ "$count" -gt 1 ]]; then
    fail "找到多個 csp-db 容器。dev／prod 同機時請設 ANILA_DB_CONTAINER，不要猜第一個"
  fi
  match=$(printf "%s\n" "$matches" | head -n1)
  printf "%s\n" "$match"
}

CONTAINER="$(resolve_container)"
docker inspect "$CONTAINER" >/dev/null 2>&1 \
  || fail "容器不存在或未啟動:（已省略名稱到 stderr）請檢查 ANILA_DB_CONTAINER"

mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/monthly"
STAMP="$(date '+%Y%m%d-%H%M%S')"
# 檔名不含主機／密碼／project 名，避免誤洩
OUT_DAILY="$BACKUP_DIR/daily/csp-${STAMP}.dump"
TMP_OUT="${OUT_DAILY}.partial"

log "開始備份 → daily/csp-${STAMP}.dump（容器已解析，細節不寫入檔名）"
START_EPOCH="$(date +%s)"

# custom format：可平行還原、單一檔即完整 artifact；-Fc 內建壓縮。
# 不經 shell 管道寫密碼；錯誤訊息若含連線字串由 pg_dump 自己決定——我們不 echo 環境。
if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-password \
  >"$TMP_OUT"; then
  rm -f "$TMP_OUT"
  fail "pg_dump 失敗（exit $?）"
fi

mv -f "$TMP_OUT" "$OUT_DAILY"
END_EPOCH="$(date +%s)"
ELAPSED=$((END_EPOCH - START_EPOCH))

# 簡單完整性：檔案非空且 pg_restore 列得出 TOC（不動目標庫）
count_toc() {
  local n
  n="$(docker run --rm \
    -v "$(dirname "$1"):/backup:ro" \
    pgvector/pgvector:pg16 \
    pg_restore -l "/backup/$(basename "$1")" 2>/dev/null | wc -l)"
  n="$(printf '%s' "$n" | tr -d '[:space:]')"
  [[ -n "$n" ]] || n=0
  printf '%s\n' "$n"
}
SIZE_BYTES="$(wc -c <"$OUT_DAILY" | tr -d '[:space:]')"
SIZE_H="$(du -h "$OUT_DAILY" | awk '{print $1}')"
[[ "${SIZE_BYTES}" -gt 1000 ]] || fail "備份檔過小（${SIZE_BYTES} bytes）"
TOC_LINES="$(count_toc "$OUT_DAILY")"
[[ "${TOC_LINES}" -ge 5 ]] || fail "備份檔 TOC 異常（${TOC_LINES} 行），已保留檔案供檢查：$OUT_DAILY"

# 每月 1 號另留一份（硬連結省空間；跨 FS 則 cp）
DAY_OF_MONTH="$(date '+%d')"
if [[ "$DAY_OF_MONTH" == "01" ]]; then
  MONTH_STAMP="$(date '+%Y%m')"
  MONTHLY="$BACKUP_DIR/monthly/csp-${MONTH_STAMP}.dump"
  if [[ ! -e "$MONTHLY" ]]; then
    ln "$OUT_DAILY" "$MONTHLY" 2>/dev/null || cp -f "$OUT_DAILY" "$MONTHLY"
    log "已寫入月初副本 monthly/csp-${MONTH_STAMP}.dump"
  fi
fi

# 保留策略：刪過舊的 daily／monthly（只刪本腳本命名的 csp-*.dump）
find "$BACKUP_DIR/daily" -type f -name 'csp-*.dump' -mtime "+${KEEP_DAYS}" -print -delete \
  | while read -r f; do log "刪除過期 daily: $(basename "$f")"; done || true

# monthly：依檔名年月排序，超過 KEEP_MONTHLY 刪最舊
mapfile -t MONTHLY_FILES < <(ls -1 "$BACKUP_DIR/monthly"/csp-*.dump 2>/dev/null | sort || true)
if ((${#MONTHLY_FILES[@]} > KEEP_MONTHLY)); then
  DROP_COUNT=$((${#MONTHLY_FILES[@]} - KEEP_MONTHLY))
  for ((i=0; i<DROP_COUNT; i++)); do
    log "刪除過期 monthly: $(basename "${MONTHLY_FILES[$i]}")"
    rm -f "${MONTHLY_FILES[$i]}"
  done
fi

log "完成：size=${SIZE_H} (${SIZE_BYTES} bytes) elapsed=${ELAPSED}s toc_entries≈${TOC_LINES}"
printf 'BACKUP_OK path=%s size_bytes=%s elapsed_s=%s\n' "$OUT_DAILY" "$SIZE_BYTES" "$ELAPSED"
