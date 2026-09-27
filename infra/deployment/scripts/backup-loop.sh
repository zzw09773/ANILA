#!/bin/sh
# compose backup 服務的主行程：先立刻跑一輪，成功後睡 24 小時。
# 失敗約 15 分鐘後再試，避免資料庫剛好重啟就空等一整天。
# ANILA_BACKUP_MAX_RUNS=1 給測試用，跑完就退出、不睡。
set -eu

HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
# shellcheck disable=SC1091
. "$HERE/backup-lib.sh"

runs=0
max=${ANILA_BACKUP_MAX_RUNS:-0}
interval=${ANILA_BACKUP_INTERVAL:-86400}
retry=${ANILA_BACKUP_RETRY_SECONDS:-900}

while true; do
  if backup_once; then
    ok=1
  else
    ok=0
    printf '%s\n' "backup run failed" >&2
  fi
  runs=$((runs + 1))
  if [ "$max" -gt 0 ] && [ "$runs" -ge "$max" ]; then
    if [ "$ok" -eq 1 ]; then
      exit 0
    fi
    exit 1
  fi
  if [ "$ok" -eq 1 ]; then
    sleep "$interval"
  else
    sleep "$retry"
  fi
done
