#!/usr/bin/env bash
# ============================================================================
# restore-all.sh — 把一份備份快照的資料庫與檔案還原回來
# ----------------------------------------------------------------------------
# 快照目錄是 share/backups/daily/YYYYMMDD-HHMMSS（或 monthly/YYYYMM）。
# 資料庫步驟交給同目錄的 restore-csp-db.sh（保留擁有權、跑不變式）。
#
# 必要：
#   ANILA_RESTORE_CONFIRM=yes
#   ANILA_RESTORE_CONTAINER   目標 Postgres 容器（不會猜 production）
#   參數：快照目錄
#
# 檔案會解進 repo 的 share/（上傳、附件、靜態檔、CA、快速起步 profile），
# 具名 volume 用 docker 解回去。先停掉會寫這些路徑的服務再跑。
# ============================================================================
set -euo pipefail

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

SNAPSHOT="${1:-${ANILA_RESTORE_SNAPSHOT:-}}"
[[ -n "$SNAPSHOT" ]] || fail "用法：ANILA_RESTORE_CONFIRM=yes ANILA_RESTORE_CONTAINER=<csp-db 容器> bash infra/deployment/scripts/restore-all.sh <快照目錄>"
[[ "${ANILA_RESTORE_CONFIRM:-}" == "yes" ]] || fail "這會寫入資料庫與檔案。確認後加上 ANILA_RESTORE_CONFIRM=yes"

[[ -d "$SNAPSHOT" ]] || fail "找不到快照目錄"
SNAPSHOT="$(cd "$SNAPSHOT" && pwd)"

need=(
  db.dump
  files-uploads.tar
  files-attachments.tar
  files-static.tar
  files-pki.tar
  files-quickstart.tar
  files-studio-artifacts.tar
  files-router-sessions.tar
  files-n8n.tar
)
for f in "${need[@]}"; do
  [[ -f "$SNAPSHOT/$f" ]] || fail "快照缺少 $f"
done

# 任何寫入之前先確認每一份封存讀得回去。壞掉的附件 tar 不能發生在
# 資料庫已經被換掉之後。
dump_magic=$(dd if="$SNAPSHOT/db.dump" bs=5 count=1 2>/dev/null || true)
[[ "$dump_magic" == "PGDMP" ]] || fail "db.dump 不是 pg_dump 自訂格式，尚未寫入"
# --list 讀完整份目錄，不是只看前五個位元組。主機沒有 pg_restore 時用映像裡的。
if command -v pg_restore >/dev/null 2>&1; then
  pg_restore --list "$SNAPSHOT/db.dump" >/dev/null \
    || fail "db.dump 目錄無法讀取，尚未寫入"
else
  image="${ANILA_BACKUP_IMAGE:-anila-pgvector:local}"
  docker run --rm --entrypoint pg_restore \
    -v "${SNAPSHOT}:/src:ro" \
    "$image" \
    --list /src/db.dump >/dev/null \
    || fail "db.dump 目錄無法讀取，尚未寫入"
fi
for f in "${need[@]}"; do
  [[ "$f" == "db.dump" ]] && continue
  tar -tf "$SNAPSHOT/$f" >/dev/null || fail "$f 無法讀取，尚未寫入"
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SHARE_ROOT="${ANILA_SHARE_ROOT:-$REPO_ROOT/share}"

log "還原快照 $(basename "$SNAPSHOT")"
log "請先停掉會寫入這些路徑的服務（csp、ingestion-worker、anila-studio、router）"

if [[ "${ANILA_RESTORE_SKIP_DB:-}" != "1" ]]; then
  export ANILA_RESTORE_DUMP="$SNAPSHOT/db.dump"
  bash "$SCRIPT_DIR/restore-csp-db.sh"
else
  log "略過資料庫（ANILA_RESTORE_SKIP_DB=1）"
fi

extract_bind() {
  local tarfile=$1 dest=$2
  mkdir -p "$dest"
  tar -xzf "$tarfile" -C "$dest"
}

log "解檔案到 ${SHARE_ROOT}"
extract_bind "$SNAPSHOT/files-uploads.tar" "$SHARE_ROOT/uploads"
extract_bind "$SNAPSHOT/files-attachments.tar" "$SHARE_ROOT/attachments"
extract_bind "$SNAPSHOT/files-static.tar" "$SHARE_ROOT/static"
extract_bind "$SNAPSHOT/files-pki.tar" "$SHARE_ROOT/pki"
extract_bind "$SNAPSHOT/files-quickstart.tar" "$SHARE_ROOT/quickstart"

if [[ "${ANILA_RESTORE_SKIP_VOLUMES:-}" != "1" ]]; then
  project="${ANILA_COMPOSE_PROJECT:-anila}"
  image="${ANILA_BACKUP_IMAGE:-anila-pgvector:local}"
  extract_volume() {
    local tarname=$1 vol=$2
    log "解 volume ${vol}"
    docker run --rm --entrypoint tar \
      -v "${vol}:/dest" \
      -v "${SNAPSHOT}:/src:ro" \
      "$image" \
      -xzf "/src/${tarname}" -C /dest
  }
  extract_volume files-studio-artifacts.tar \
    "${ANILA_STUDIO_VOLUME:-${project}_anila-studio-artifacts}"
  extract_volume files-router-sessions.tar \
    "${ANILA_ROUTER_VOLUME:-${project}_router-sessions}"
  extract_volume files-n8n.tar \
    "${ANILA_N8N_VOLUME:-${project}_n8n_data}"
else
  log "略過具名 volume（ANILA_RESTORE_SKIP_VOLUMES=1）"
fi

log "檔案與資料庫步驟都跑完了。接下來把 csp_app 密碼設回、啟動服務，並依 runbook 核對登入、舊對話、附件與已索引文件。"
printf 'RESTORE_ALL_OK\n'
