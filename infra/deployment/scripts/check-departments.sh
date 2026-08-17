#!/usr/bin/env bash
# 唯讀部署後提醒:確認 departments 至少有一個啟用中的單位。
#
# 參數會原樣放在 `docker compose` 後面,供 intranet bundle 路徑傳入
# project、overlay 與 profile；一般 deploy-prod 路徑不需參數。
# 這支檢查刻意永遠以 0 結束:它是初裝提醒,不是部署閘門。
set -u

warn() { printf '  ⚠️  %s\n' "$*"; }

count=''
if ! count="$(docker compose "$@" exec -T csp-db \
    psql -U csp -d csp -Atqc \
    'SELECT count(*) FROM departments WHERE is_active IS TRUE;' 2>/dev/null)"; then
  warn '無法檢查啟用中的單位（departments）；部署將繼續。請資料庫可用後依 docs/runbooks/first-install-rehearsal.md §2.1 確認。'
  exit 0
fi

count="${count//$'\r'/}"
count="${count//[[:space:]]/}"
if [[ "$count" == 0 ]]; then
  warn '初裝提醒：目前沒有任何啟用中的單位（departments），員工因此無法完成註冊。請依 docs/runbooks/first-install-rehearsal.md §2.1，由 owner 登入後到 /departments 建立單位清單。部署會繼續。'
elif [[ ! "$count" =~ ^[1-9][0-9]*$ ]]; then
  warn '無法判讀啟用中的單位數量；部署將繼續。請資料庫可用後依 docs/runbooks/first-install-rehearsal.md §2.1 確認。'
fi

exit 0
