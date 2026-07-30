#!/usr/bin/env bash
# ============================================================================
# restore-csp-db.sh — 把 backup-csp-db.sh 的 .dump 還原到指定 Postgres 容器
# ----------------------------------------------------------------------------
# 給凌晨事故用：目標容器名必須明示，不會猜 production。
# 密碼只從環境變數讀，不寫入 log、不寫入檔名。
#
# 必要：
#   ANILA_RESTORE_CONTAINER  目標 pg 容器名
#   ANILA_RESTORE_DUMP       .dump 絕對路徑
#
# 可選：
#   ANILA_DB_NAME / ANILA_DB_USER     預設 csp / csp
#
# 還原後若要讓平台 csp 服務連得上，請依 runbook 手動 ALTER ROLE csp_app
# PASSWORD（值來自 .env 的 CSP_APP_DB_PASSWORD）。本腳本不碰密碼。
# ============================================================================
set -euo pipefail

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "ERROR: $*"; exit 1; }

CONTAINER="${ANILA_RESTORE_CONTAINER:?ANILA_RESTORE_CONTAINER must be set}"
DUMP="${ANILA_RESTORE_DUMP:?ANILA_RESTORE_DUMP must be set}"
DB_NAME="${ANILA_DB_NAME:-csp}"
DB_USER="${ANILA_DB_USER:-csp}"

[[ -f "$DUMP" ]] || fail "找不到 dump 檔"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "目標容器不存在或未啟動"

# dump 含授予 csp_app 的 ACL；目標必須先有這個 role，否則 pg_restore 滿屏 ERROR。
log "確保 role csp_app 存在（尚不設密碼；上線前見 runbook）"
docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  -c "DO \$\$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'csp_app') THEN
          CREATE ROLE csp_app LOGIN;
        END IF;
      END \$\$;"

WORKDIR="$(mktemp -d /tmp/anila-restore.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT
cp "$DUMP" "$WORKDIR/restore.dump"

log "開始 pg_restore"
set +e
docker run --rm --network "container:${CONTAINER}" \
  -v "$WORKDIR:/backup:ro" \
  pgvector/pgvector:pg16 \
  pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
    --no-owner --role="$DB_USER" \
    /backup/restore.dump \
  >/tmp/anila-restore-pg.out 2>/tmp/anila-restore-pg.err
RC=$?
set -e

if grep -qE '^pg_restore: error:' /tmp/anila-restore-pg.err; then
  log "pg_restore 仍有 error（exit=$RC）；不重複的摘要："
  grep -E '^pg_restore: error:' /tmp/anila-restore-pg.err | sort -u | head -n 20
  fail "還原未乾淨結束，詳見 /tmp/anila-restore-pg.err"
fi

log "還原完成（pg_restore exit=$RC）。核對列數："
log "  docker exec ${CONTAINER} psql -U ${DB_USER} -d ${DB_NAME} -c \"SELECT count(*) FROM audit_logs;\""
log "  docker exec ${CONTAINER} psql -U ${DB_USER} -d ${DB_NAME} -c \"SELECT count(*) FROM token_usage;\""
printf 'RESTORE_OK\n'
