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
RESTORE_OUT="$(mktemp /tmp/anila-restore-pg.XXXXXX.out)"
RESTORE_ERR="$(mktemp /tmp/anila-restore-pg.XXXXXX.err)"
# 成功才清 log；失敗保留，讓「詳見 $RESTORE_ERR」真的指得到檔案。
_RESTORE_KEEP_LOGS=0
cleanup() {
  rm -rf "$WORKDIR"
  if [[ "$_RESTORE_KEEP_LOGS" -eq 0 ]]; then
    rm -f "$RESTORE_OUT" "$RESTORE_ERR"
  fi
}
trap cleanup EXIT
cp "$DUMP" "$WORKDIR/restore.dump"

log "開始 pg_restore（保留 dump 內的擁有權，見下方說明）"
# ⚠ 刻意**不用** `--no-owner --role=csp`。
#
# `--no-owner` 會讓所有物件都變成連線者（csp）所有。那會一次砸壞兩件事：
#   1. 平台開機時 `startup_migrations` 會對 users / model_registry /
#      token_usage / departments / api_keys / alerts / platform_links /
#      attachments 發 ALTER TABLE 與 CREATE INDEX。那些 DDL 需要**擁有權**，
#      而 0014 是刻意把它們給 csp_app 的。還原成 csp 所有之後，開機會噴
#      `must be owner of table users`，而且那個例外被 run_startup_migrations
#      吞掉 → **/health 回 200、容器顯示 healthy、但使用者登不進來**。
#      （2026-07-31 實測重現：auto_seed 也跟著炸 `users.token_version does not exist`。）
#   2. 稽核家族（P2.7）需要的正好相反：**不可以**是 csp_app 所有。
# 一句話：正確狀態不是「全歸某一個 role」，而是 dump 當下那張擁有權地圖。
# 保留它最簡單也最忠實；之後再用 assert-db-ownership.sql 補強不變式。
set +e
docker run --rm --network "container:${CONTAINER}" \
  -v "$WORKDIR:/backup:ro" \
  pgvector/pgvector:pg16 \
  pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" \
    /backup/restore.dump \
  >"$RESTORE_OUT" 2>"$RESTORE_ERR"
RC=$?
set -e

# docker run 本身失敗時 stderr 是 `docker: Error ...`，不會有
# `pg_restore: error:` 前綴——必須先驗 RC，否則空庫姿態檢查會誤報成功。
if [[ "$RC" -ne 0 ]]; then
  _RESTORE_KEEP_LOGS=1
  log "pg_restore／docker run 失敗（exit=$RC）；stderr 見 $RESTORE_ERR"
  log "ERROR: 還原未啟動或未完成（exit=$RC），詳見 $RESTORE_ERR"
  exit "$RC"
fi

if grep -qE '^pg_restore: error:' "$RESTORE_ERR"; then
  _RESTORE_KEEP_LOGS=1
  log "pg_restore 仍有 error（exit=$RC）；不重複的摘要："
  grep -E '^pg_restore: error:' "$RESTORE_ERR" | sort -u | head -n 20
  fail "還原未乾淨結束，詳見 $RESTORE_ERR"
fi

# ── 擁有權/權限不變式（自動，操作者不需要記得任何事） ──────────────────
SQL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNERSHIP_SQL="$SQL_DIR/assert-db-ownership.sql"
[[ -f "$OWNERSHIP_SQL" ]] || fail "找不到 assert-db-ownership.sql（同目錄）"
log "校正擁有權與稽核表權限（assert-db-ownership.sql；正常情況下不會有任何變動）"
docker exec -i "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
  < "$OWNERSHIP_SQL" || fail "擁有權校正失敗，請勿把平台指向這個庫"

# 硬閘：上面跑完還不對就不要說 RESTORE_OK。凌晨兩點最不需要的就是一個
# 「還原成功」但開機起來是壞的資料庫。
log "驗收還原後的姿態"
BAD="$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -c "
  SELECT string_agg(msg, '; ') FROM (
    SELECT c.relname || ' 應歸 csp_app 但歸 ' || pg_get_userbyid(c.relowner) AS msg
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname='public' AND c.relkind='r'
       AND c.relname IN ('users','model_registry','token_usage','departments',
                         'api_keys','alerts','platform_links','attachments')
       AND pg_get_userbyid(c.relowner) <> 'csp_app'
    UNION ALL
    SELECT c.relname || ' 是稽核表但歸 csp_app（P2.7 失效）'
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname='public' AND c.relkind='r'
       AND c.relname IN ('audit_logs','policy_decisions',
                         'classification_events','audit_checkpoints')
       AND pg_get_userbyid(c.relowner) = 'csp_app'
    UNION ALL
    SELECT c.relname || ': csp_app 仍能 ' || p
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
           unnest(ARRAY['UPDATE','DELETE','TRUNCATE']) p
     WHERE n.nspname='public' AND c.relkind='r'
       AND c.relname IN ('audit_logs','policy_decisions',
                         'classification_events','audit_checkpoints')
       AND has_table_privilege('csp_app', c.oid, p)
  ) t;")"
if [[ -n "${BAD// /}" ]]; then
  log "還原後姿態不合格：$BAD"
  fail "不要把平台指向這個庫。若這是 r1_0027 之前的舊 dump，先讓 csp 開機跑完 alembic 再重驗。"
fi
log "姿態 OK：開機 DDL 用的表歸 csp_app；稽核四表不歸 csp_app 且不可改/刪"

log "還原完成（pg_restore exit=$RC）。核對列數："
log "  docker exec ${CONTAINER} psql -U ${DB_USER} -d ${DB_NAME} -c \"SELECT count(*) FROM audit_logs;\""
log "  docker exec ${CONTAINER} psql -U ${DB_USER} -d ${DB_NAME} -c \"SELECT count(*) FROM token_usage;\""
log "⚠ P2.7：還原之後**必跑**稽核鏈驗證，並拿一份已經交出去的稽核匯出檔上印的"
log "  鏈頭來比對 —— 被調換或被回捲的備份，鏈頭會對不上："
log "  docker exec <csp 容器> python scripts/verify_audit_chain.py --head <報告上的鏈頭>"
log "  （沒有 --head 只能驗自洽；鏈頭不在 dump 裡，那正是它有用的原因。"
log "   請確認你手上留著至少一份已發出的稽核匯出檔，見 runbook §2.7。）"
printf 'RESTORE_OK\n'
