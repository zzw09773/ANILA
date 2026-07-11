#!/usr/bin/env bash
# ============================================================================
# anila-ops.sh — ANILA 平台日常維運腳本 (prod)
# ----------------------------------------------------------------------------
# 定位:部署「之後」的日常維運。部署本體用:
#   - 首次/重大部署: infra/deployment/intranet/intranet-deploy.sh (一條龍)
#   - 起停/重建:     infra/deployment/scripts/deploy-prod.sh {up|down|rebuild|...}
#
# 用法 (在 repo 根目錄,例 /opt/anila):
#   bash infra/deployment/scripts/anila-ops.sh <subcommand> [args]
#
# SUBCOMMAND:
#   status                     stack + 模型 stack 一覽 (等同 deploy-prod.sh status)
#   health                     深度健檢:容器/端點/模型鏈路/TLS 效期/磁碟/備份新鮮度
#   logs <svc> [n]             tail -f 單一 service logs (預設最後 100 行)
#   backup [--full]            備份 DB + .env + secrets + pki;--full 加上傳檔案
#   restore <backup-dir>       從 backup 目錄還原 DB (毀滅性,需輸入 RESTORE 確認)
#   cert-renew <pfx>           平台 TLS 換發:安全提示密碼後重抽 fullchain+key → nginx reload
#   model-ca <pem>             更新模型 gateway 出向 CA → 重建 csp
#   gateway-key                輪替 MODEL_GATEWAY_API_KEY → 重建 csp → 探測
#   break-glass on|off         讀卡環境故障應急:on=暫開帳密登入 / off=恢復 card-only
#   prune                      清 dangling image + builder cache (不碰 volume)
#   help                       顯示這份說明
#
# 鐵則 (寫死在本腳本的行為,不要繞過):
#   1. 套用 .env / 掛載檔變更一律 `docker compose up -d [--force-recreate]`,
#      絕不用 `docker restart` (不重載 env,見 AGENTS.md §4)。
#   2. 本腳本不動 ANILA_ALLOW_* / ANILA_ENV 等 strict 旗標
#      (唯一例外 = break-glass 的 REQUIRE_CARD_LOGIN_ONLY,那是文件化的應急程序)。
#   3. 備份輸出含機密 (.env / JWT 私鑰 / DB dump):目錄 700、檔案 600,
#      預設寫到 repo 外的 user state 目錄,且拒絕任何落在 repo 內的覆寫。
#
# 環境變數 (可選):
#   ANILA_STATE_DIR    ANILA 外部 state 根目錄
#                      (預設 $XDG_STATE_HOME/anila 或 $HOME/.local/state/anila)
#   ANILA_BACKUP_DIR   備份根目錄 (預設 $ANILA_STATE_DIR/backups;必須在 repo 外)
#   ANILA_BACKUP_KEEP  保留最近幾份備份 (預設 14)
# ============================================================================
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"
get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

if [ -n "${XDG_STATE_HOME:-}" ]; then
  DEFAULT_STATE_DIR="$XDG_STATE_HOME/anila"
elif [ -n "${HOME:-}" ]; then
  DEFAULT_STATE_DIR="$HOME/.local/state/anila"
else
  DEFAULT_STATE_DIR="/var/lib/anila"
fi
_saved_state="$(get_env ANILA_STATE_DIR)"
_saved_secrets="$(get_env ANILA_SECRETS_DIR)"
_saved_tls="$(get_env ANILA_TLS_CERTS_DIR)"
if [ -n "${ANILA_STATE_DIR:-}" ] && [ -n "$_saved_state" ] && [ "$ANILA_STATE_DIR" != "$_saved_state" ]; then
  printf 'ANILA_STATE_DIR 與既有 .env 不一致；拒絕靜默切換 private-key state root\n' >&2; exit 1
fi
if [ -n "${ANILA_SECRETS_DIR:-}" ] && [ -n "$_saved_secrets" ] && [ "$ANILA_SECRETS_DIR" != "$_saved_secrets" ]; then
  printf 'ANILA_SECRETS_DIR 與既有 .env 不一致\n' >&2; exit 1
fi
if [ -n "${ANILA_TLS_CERTS_DIR:-}" ] && [ -n "$_saved_tls" ] && [ "$ANILA_TLS_CERTS_DIR" != "$_saved_tls" ]; then
  printf 'ANILA_TLS_CERTS_DIR 與既有 .env 不一致\n' >&2; exit 1
fi
ANILA_STATE_DIR="${ANILA_STATE_DIR:-${_saved_state:-$DEFAULT_STATE_DIR}}"
BACKUP_DIR="${ANILA_BACKUP_DIR:-$ANILA_STATE_DIR/backups}"
SECRETS_DIR="${ANILA_SECRETS_DIR:-$_saved_secrets}"
TLS_CERTS_DIR="${ANILA_TLS_CERTS_DIR:-$_saved_tls}"
SECRETS_DIR="${SECRETS_DIR:-$ANILA_STATE_DIR/secrets}"
TLS_CERTS_DIR="${TLS_CERTS_DIR:-$ANILA_STATE_DIR/tls}"
BACKUP_KEEP="${ANILA_BACKUP_KEEP:-14}"
CSP_RUNTIME_IMAGE="anila-platform-csp:latest"
SAFE_RUNTIME_BACKUP_HELPER="$REPO_ROOT/infra/deployment/scripts/safe-runtime-backup.py"

# ── 輸出 helper ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_C=$'\033[36m'; C_N=$'\033[0m'
else
  C_R=""; C_G=""; C_Y=""; C_C=""; C_N=""
fi
log()   { printf "%s[%s]%s %s\n" "$C_C" "$(date +%H:%M:%S)" "$C_N" "$*"; }
ok()    { printf "  %s✓%s %s\n" "$C_G" "$C_N" "$*"; }
warn()  { printf "  %s⚠%s %s\n" "$C_Y" "$C_N" "$*"; }
err()   { printf "  %s✗%s %s\n" "$C_R" "$C_N" "$*"; }
fatal() { err "$*"; exit 1; }
section(){ printf "\n%s── %s ──%s\n" "$C_C" "$*" "$C_N"; }

# 備份含全庫 dump 與正式 secret。即使 operator 覆寫路徑,也不可重新落進
# repo (code-server / IDE / accidental archive 的可見範圍)。
assert_outside_repo() {
  local raw="$1" label="$2" repo_real target_real
  command -v realpath >/dev/null 2>&1 || fatal "缺少 realpath,無法驗證 $label 路徑"
  repo_real="$(realpath -m -- "$REPO_ROOT")"
  target_real="$(realpath -m -- "$raw")"
  case "$target_real" in
    "$repo_real"|"$repo_real"/*)
      fatal "$label 不得位於 repo 內: $target_real (請設到獨立受控磁碟/目錄)" ;;
  esac
  OUTSIDE_PATH="$target_real"
}
assert_safe_state_layout() {
  local state_lex state_real target_lex target_real label raw leaf
  [[ "$ANILA_STATE_DIR" = /* ]] || fatal "ANILA_STATE_DIR 必須是絕對路徑"
  state_lex="$(realpath -ms -- "$ANILA_STATE_DIR")"
  state_real="$(realpath -m -- "$ANILA_STATE_DIR")"
  [[ "$state_lex" == "$state_real" ]] || fatal "ANILA_STATE_DIR 不得包含 symlink component"
  case "$state_real" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib)
      fatal "ANILA_STATE_DIR 太寬或是系統目錄: $state_real" ;;
  esac
  assert_outside_repo "$state_real" ANILA_STATE_DIR
  ANILA_STATE_DIR="$state_real"
  for label in ANILA_SECRETS_DIR ANILA_TLS_CERTS_DIR; do
    if [ "$label" = ANILA_SECRETS_DIR ]; then raw="$SECRETS_DIR"; leaf=secrets; else raw="$TLS_CERTS_DIR"; leaf=tls; fi
    [[ "$raw" = /* ]] || fatal "$label 必須是絕對路徑"
    target_lex="$(realpath -ms -- "$raw")"; target_real="$(realpath -m -- "$raw")"
    [[ "$target_lex" == "$target_real" ]] || fatal "$label 不得包含 symlink component"
    [[ "$target_real" == "$state_real/$leaf" ]] || fatal "$label 必須固定為 $state_real/$leaf"
    if [ "$label" = ANILA_SECRETS_DIR ]; then SECRETS_DIR="$target_real"; else TLS_CERTS_DIR="$target_real"; fi
  done
}
OUTSIDE_PATH=""
assert_safe_state_layout
export ANILA_STATE_DIR
export ANILA_SECRETS_DIR="$SECRETS_DIR"
export ANILA_TLS_CERTS_DIR="$TLS_CERTS_DIR"
assert_outside_repo "$BACKUP_DIR" ANILA_BACKUP_DIR
BACKUP_DIR="$OUTSIDE_PATH"

# ── .env helper (同 intranet-deploy.sh:literal 去重 append,不用 sed 跳脫) ──
set_env() {
  local key="$1" val="$2" tmp
  [ -f .env ] && [ ! -L .env ] || fatal ".env 必須是 regular file 且不得是 symlink"
  tmp="$(mktemp "$REPO_ROOT/.env.tmp.XXXXXX")" || fatal "無法建立安全 .env temp file"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv -f -- "$tmp" .env
}

set_env_single_quoted() {
  local key="$1" val="$2" tmp
  [[ "$val" != *"'"* && "$val" != *$'\n'* && "$val" != *$'\r'* ]] \
    || fatal "$key 含不能安全寫入 .env 單引號值的字元"
  [ -f .env ] && [ ! -L .env ] || fatal ".env 必須是 regular file 且不得是 symlink"
  tmp="$(mktemp "$REPO_ROOT/.env.tmp.XXXXXX")" || fatal "無法建立安全 .env temp file"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  printf "%s='%s'\n" "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv -f -- "$tmp" .env
}

need_stack() {
  command -v docker >/dev/null || fatal "找不到 docker"
  docker info >/dev/null 2>&1  || fatal "docker daemon 沒在跑 / 當前使用者無權限"
  [ -f compose.yaml ]          || fatal "請在 repo 根目錄執行 (找不到 compose.yaml)"
}

# ── status ─────────────────────────────────────────────────────────────────
cmd_status() {
  need_stack
  section "anila-platform stack"
  docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Image}}"
  section "anila-models stack (若本機有跑模型)"
  docker compose -p anila-models ps --format "table {{.Service}}\t{{.Status}}" 2>/dev/null \
    || warn "anila-models project 沒 running (純 gateway 模式屬正常)"
}

# ── health ─────────────────────────────────────────────────────────────────
_FAILS=0
chk() {  # chk <ok|warn|fail> <訊息>
  case "$1" in
    ok)   ok "$2" ;;
    warn) warn "$2" ;;
    fail) err "$2"; _FAILS=$((_FAILS + 1)) ;;
  esac
}

health_containers() {
  section "容器健康"
  local svcs=(csp-db redis csp ingestion-worker router nginx anilalm anila-ui pptx-renderer anila-studio flux2-dev-agent n8n gitlab)
  local s cid st hs
  for s in "${svcs[@]}"; do
    cid="$(docker compose ps -q "$s" 2>/dev/null || true)"
    if [ -z "$cid" ]; then chk fail "$s: 沒 running"; continue; fi
    st="$(docker inspect "$cid" --format '{{.State.Status}}' 2>/dev/null || echo '?')"
    hs="$(docker inspect "$cid" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-hc{{end}}' 2>/dev/null || echo '?')"
    case "$hs" in
      healthy)   chk ok   "$s: healthy" ;;
      no-hc)     [ "$st" = running ] && chk ok "$s: running (無 healthcheck)" || chk fail "$s: $st" ;;
      starting)  chk warn "$s: starting (尚未就緒)" ;;
      *)         chk fail "$s: $hs ($st)" ;;
    esac
  done
}

health_endpoints() {
  section "服務端點"
  # nginx → csp (host 對外唯一入口)
  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 https://localhost/health 2>/dev/null || echo fail)
  [ "$code" = 200 ] && chk ok "https://localhost/health → 200" \
                    || chk fail "https://localhost/health → $code"
  # csp 容器內 (csp image 沒 curl,用 python urllib;curl 回空會是假陰性)
  if docker compose exec -T csp python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=5)" >/dev/null 2>&1; then
    chk ok "csp /ready (internal) → 200"
  else
    chk fail "csp /ready (internal) 失敗"
  fi
  # JWKS:缺 JWT keypair 時這裡會 500 (登入發不了 token、studio crash-loop 的前兆)
  if docker compose exec -T csp python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/.well-known/jwks.json', timeout=5)" >/dev/null 2>&1; then
    chk ok "csp /.well-known/jwks.json → 200 (JWT 簽章金鑰正常)"
  else
    chk fail "csp JWKS 失敗 — 檢查 $SECRETS_DIR/jwt-private.pem 是否存在"
  fi
  docker compose exec -T router curl -sf --max-time 5 http://localhost:9000/ready >/dev/null 2>&1 \
    && chk ok "router /ready (internal) → 200" || chk fail "router /ready (internal) 失敗"
  docker compose exec -T anila-studio curl -sf --max-time 5 http://localhost:8100/health >/dev/null 2>&1 \
    && chk ok "anila-studio /health (internal) → 200" || chk fail "anila-studio /health (internal) 失敗"
  docker compose exec -T csp-db pg_isready -U csp -d csp >/dev/null 2>&1 \
    && chk ok "postgres pg_isready OK" || chk fail "postgres pg_isready 失敗"
  [ "$(docker compose exec -T redis redis-cli ping 2>/dev/null | tr -d '\r')" = PONG ] \
    && chk ok "redis PONG" || chk fail "redis ping 失敗"
}

health_model_gateway() {
  section "模型鏈路 (出向 gateway)"
  local base key ca code auth=()
  base="$(get_env LOCAL_LLM_BASE_URL)"; key="$(get_env MODEL_GATEWAY_API_KEY)"
  ca=share/pki/model-ca.pem
  if [ -z "$base" ]; then chk warn "LOCAL_LLM_BASE_URL 未設 — 跳過 gateway 探測"; return; fi
  [ -n "$key" ] && auth=(-H "Authorization: Bearer $key") \
                || chk warn "MODEL_GATEWAY_API_KEY 未設 — gateway /v1 會 401 (key 還沒簽發?)"
  if [ -s "$ca" ]; then
    code=$(curl -s --cacert "$ca" -o /dev/null -w '%{http_code}' --max-time 8 \
           ${auth[@]+"${auth[@]}"} "${base%/}/v1/models" 2>/dev/null || echo fail)
  else
    code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 \
           ${auth[@]+"${auth[@]}"} "${base%/}/v1/models" 2>/dev/null || echo fail)
    chk warn "$ca 不存在 — 以 -k 探測 (csp 容器內仍需正式 CA 驗證)"
  fi
  case "$code" in
    200) chk ok   "gateway ${base%/}/v1/models → 200" ;;
    401|403) chk fail "gateway → $code (key 失效或沒帶到)" ;;
    *)   chk fail "gateway → $code (服務沒起 / 防火牆 / DNS / TLS)" ;;
  esac
}

health_certs() {
  section "憑證效期"
  local crt="$TLS_CERTS_DIR/server.crt" ca=share/pki/model-ca.pem f
  for f in "$crt" "$ca"; do
    if [ ! -s "$f" ]; then chk warn "$f 不存在"; continue; fi
    if openssl x509 -in "$f" -noout -checkend $((30*86400)) >/dev/null 2>&1; then
      chk ok "$f 效期 > 30 天 ($(openssl x509 -in "$f" -noout -enddate | cut -d= -f2))"
    elif openssl x509 -in "$f" -noout -checkend 0 >/dev/null 2>&1; then
      chk warn "$f 將在 30 天內到期 — 排程換發 (cert-renew / model-ca)"
    else
      chk fail "$f 已過期"
    fi
  done
  # secrets/ is UID 10001 + mode 0700,so the host operator must not stat it.
  # Ask the running non-root CSP process to load the exact key files instead.
  if docker compose exec -T csp python -c \
      "from app.utils.security import get_private_key,get_public_key; assert get_private_key() and get_public_key()" \
      >/dev/null 2>&1; then
    chk ok "JWT keypair 可由 CSP runtime 載入"
  else
    chk fail "JWT keypair 無法由 CSP runtime 載入 — 見 deploy-prod.sh ensure_jwt_keypair"
  fi
}

health_disk_backup() {
  section "磁碟與備份"
  df -h "$REPO_ROOT" | tail -1 | awk '{printf "  磁碟 (%s): 已用 %s / %s (%s)\n", $6, $3, $2, $5}'
  local pct
  pct="$(df -P "$REPO_ROOT" | tail -1 | awk '{gsub("%","",$5); print $5}')"
  [ "${pct:-0}" -ge 85 ] && chk fail "磁碟使用率 ${pct}% ≥ 85% — 先跑 prune / 清理 share/uploads" \
                         || chk ok "磁碟使用率 ${pct}%"
  docker system df --format '  docker: {{.Type}} {{.Size}} (可回收 {{.Reclaimable}})' 2>/dev/null || true
  local latest
  latest="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | tail -1)"
  if [ -z "$latest" ]; then
    chk warn "找不到任何備份 ($BACKUP_DIR) — 建議排 cron 每日 backup"
  elif [ -n "$(find "$latest" -maxdepth 0 -mtime -7 2>/dev/null)" ]; then
    chk ok "最新備份: $latest (7 天內)"
  else
    chk warn "最新備份超過 7 天: $latest"
  fi
}

cmd_health() {
  need_stack
  log "ANILA 深度健檢"
  health_containers
  health_endpoints
  health_model_gateway
  health_certs
  health_disk_backup
  echo
  if [ "$_FAILS" -eq 0 ]; then log "健檢完成:無 FAIL 項"; else
    log "健檢完成:$_FAILS 個 FAIL 項 — 逐項處理,對照 docs/runbooks/intranet-deployment-runbook.md §6"
    exit 1
  fi
}

# ── logs ───────────────────────────────────────────────────────────────────
cmd_logs() {
  need_stack
  local svc="${1:-}"; [ -n "$svc" ] || fatal "usage: anila-ops.sh logs <svc> [n]"
  docker compose logs -f --tail="${2:-100}" "$svc"
}

# ── backup ─────────────────────────────────────────────────────────────────
assert_real_source_directory() {
  local source="$1" label="$2"
  [ ! -L "$source" ] || fatal "$label 不得是 symlink: $source"
  [ -d "$source" ] || fatal "$label 不存在或不是目錄: $source"
}

ensure_runtime_backup_helper() {
  [ -s "$SAFE_RUNTIME_BACKUP_HELPER" ] \
    || fatal "缺少安全備份 helper: $SAFE_RUNTIME_BACKUP_HELPER"
  docker image inspect "$CSP_RUNTIME_IMAGE" >/dev/null 2>&1 \
    || fatal "缺少 $CSP_RUNTIME_IMAGE — air-gap 主機請先跑 INTRANET-LOAD.sh"
}

# 以 CSP image 內的 Python 做一次性 root helper。只有精確的 read-only source
# 與當次 backup destination 會掛入；container 無網路、root filesystem 也是唯讀。
# helper 會把輸出 chown 回 host operator，避免 root-owned backup 無法輪替/搬移。
run_runtime_backup_helper() {
  local source_type="$1" source="$2" command="$3" destination="$4" kind="${5:-}"
  local source_mount uid gid
  uid="$(id -u)"; gid="$(id -g)"

  case "$source_type" in
    bind)
      assert_real_source_directory "$source" "$command backup source"
      source_mount="type=bind,source=$source,target=/source,readonly"
      ;;
    volume)
      docker volume inspect "$source" >/dev/null 2>&1 \
        || fatal "找不到 runtime volume: $source"
      source_mount="type=volume,source=$source,target=/source,readonly"
      ;;
    *) fatal "未知 runtime backup source type: $source_type" ;;
  esac

  local helper_args=("$command" --source /source --destination /backup --uid "$uid" --gid "$gid")
  [ "$command" != tree ] || helper_args+=(--kind "$kind")
  docker run --rm --pull never --user 0:0 --network none --read-only \
    --security-opt no-new-privileges \
    --mount "$source_mount" \
    --mount "type=bind,source=$destination,target=/backup" \
    "$CSP_RUNTIME_IMAGE" python - "${helper_args[@]}" \
    < "$SAFE_RUNTIME_BACKUP_HELPER" \
    || fatal "$kind$command runtime backup helper 失敗 — 備份不完整,已中止"
}

ATTACHMENT_VOLUME=""
find_attachment_volume() {
  local item count=0
  ATTACHMENT_VOLUME=""
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    ATTACHMENT_VOLUME="$item"
    count=$((count + 1))
  done < <(docker volume ls --quiet \
    --filter label=com.docker.compose.project=anila-platform \
    --filter label=com.docker.compose.volume=csp-attachments)
  [ "$count" -eq 1 ] \
    || fatal "--full 需要且只能找到 1 個 csp-attachments volume (目前 $count 個)"
}

backup_public_pki() {
  local destination="$1" pem pems=()
  [ -e "$REPO_ROOT/share/pki" ] || return 0
  assert_real_source_directory "$REPO_ROOT/share/pki" "share/pki"
  shopt -s nullglob
  pems=("$REPO_ROOT"/share/pki/*.pem)
  shopt -u nullglob
  [ "${#pems[@]}" -gt 0 ] || return 0
  mkdir -m 700 "$destination/pki"
  for pem in "${pems[@]}"; do
    [ ! -L "$pem" ] && [ -f "$pem" ] \
      || fatal "PKI 備份只接受 regular file,拒絕: $pem"
    cp -- "$pem" "$destination/pki/"
  done
  chmod 600 "$destination/pki/"*.pem
}

cmd_backup() {
  need_stack
  local full=0; [ "${1:-}" = --full ] && full=1
  [ -n "$(docker compose ps --status running -q csp-db 2>/dev/null)" ] \
    || fatal "csp-db 沒 running — 無法 pg_dump"
  ensure_runtime_backup_helper

  local stamp dest
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="$BACKUP_DIR/$stamp"
  mkdir -p -m 700 "$BACKUP_DIR"; mkdir -m 700 "$dest"
  log "備份到 $dest (含機密,目錄 700)"

  # 1. DB:pg_dump 整個 csp database (schema+data;csp_app role 是 cluster 級,
  #    不在 dump 內 — restore 到全新 volume 時由 cmd_restore 補建)。
  docker compose exec -T csp-db pg_dump -U csp -d csp | gzip > "$dest/db-csp.sql.gz"
  [ -s "$dest/db-csp.sql.gz" ] || fatal "pg_dump 輸出是空的 — 中止"
  ok "DB dump: $(du -h "$dest/db-csp.sql.gz" | cut -f1)"

  # 2. 設定與金鑰 (還原整台機器的最小集) — 這裡缺一樣都不算備份成功,fail-loud
  [ -f .env ] && [ ! -L .env ] \
    || fatal ".env 必須是 regular file 且不得是 symlink"
  cp .env "$dest/env.bak" || fatal "備份 .env 失敗 — 中止 (少了它 secret 全滅,備份不可用)"
  chmod 600 "$dest/env.bak"
  run_runtime_backup_helper bind "$SECRETS_DIR" secrets "$dest"
  run_runtime_backup_helper bind "$TLS_CERTS_DIR" tls "$dest"
  backup_public_pki "$dest"
  ok "設定/金鑰: .env + JWT secrets/ + TLS keypair + share/pki/"

  # 3. --full:使用者上傳原檔 + CSP attachment named volume。兩者都由
  #    one-shot root helper 從精確 readonly mount 讀取,tar 不跟隨 symlink。
  if [ "$full" = 1 ]; then
    run_runtime_backup_helper bind "$REPO_ROOT/share/uploads" tree "$dest" uploads
    ok "上傳檔案: $(du -h "$dest/uploads.tar.gz" | cut -f1)"
    find_attachment_volume
    run_runtime_backup_helper volume "$ATTACHMENT_VOLUME" tree "$dest" attachments
    ok "CSP 附件: $(du -h "$dest/attachments.tar.gz" | cut -f1)"
  fi

  # 4. manifest + checksum (異機還原時對版本、驗完整性)
  {
    echo "stamp: $stamp"
    echo "git:   $(git rev-parse HEAD 2>/dev/null || echo '?') ($(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?'))"
    echo "note:  n8n/gitlab volume 不在此備份內 — gitlab 用它自己的 gitlab-backup 機制"
    echo "full:  $full (1 = uploads.tar.gz + attachments.tar.gz)"
    docker compose images 2>/dev/null || true
  } > "$dest/MANIFEST.txt"
  (cd "$dest" && find . -type f ! -name CHECKSUMS.sha256 -exec sha256sum {} + > CHECKSUMS.sha256)
  chmod 600 "$dest"/CHECKSUMS.sha256 "$dest"/MANIFEST.txt

  # 5. retention:只留最近 BACKUP_KEEP 份。只認本腳本的 YYYYmmdd-HHMMSS 命名 —
  #    operator 手動放進來的其他目錄 (如異機備份) 一律不碰。
  local old
  old="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d \
         -regextype posix-extended -regex '.*/[0-9]{8}-[0-9]{6}' | sort | head -n -"$BACKUP_KEEP" || true)"
  if [ -n "$old" ]; then
    echo "$old" | while IFS= read -r d; do rm -rf "$d"; warn "retention 清除舊備份: $d"; done
  fi
  log "備份完成: $dest"
}

# ── restore ────────────────────────────────────────────────────────────────
cmd_restore() {
  need_stack
  local src="${1:-}"
  [ -n "$src" ] && [ -f "$src/db-csp.sql.gz" ] \
    || fatal "usage: anila-ops.sh restore <backup-dir> (目錄內要有 db-csp.sql.gz)"

  if [ -f "$src/CHECKSUMS.sha256" ]; then
    (cd "$src" && sha256sum -c CHECKSUMS.sha256 --quiet) || fatal "備份 checksum 驗證失敗 — 檔案可能損壞"
    ok "備份 checksum 驗證通過"
  else
    warn "$src 沒有 CHECKSUMS.sha256 — 跳過 checksum 驗證"
  fi
  # dump 完整性:truncated gzip 會在灌到一半才炸 (EOF 不是 SQL error,psql 不會擋)。
  # 動手前先整包解壓驗 trailer,壞包直接擋在門外。
  if ! gunzip -c "$src/db-csp.sql.gz" | tail -20 | grep -q 'PostgreSQL database dump complete'; then
    fatal "db-csp.sql.gz 不完整 (缺 pg_dump 結尾標記) — 拒絕還原"
  fi
  ok "dump 完整性驗證通過 (pg_dump trailer 存在)"

  warn "即將「清空並覆蓋」目前的 csp 資料庫 (使用者/對話/知識庫全部換成備份內容)"
  warn "來源: $src ($(head -2 "$src/MANIFEST.txt" 2>/dev/null | tr '\n' ' '))"
  local confirm; read -rp "  確定請輸入 RESTORE: " confirm
  [ "$confirm" = RESTORE ] || fatal "已取消"

  log "停掉會碰 DB 的服務"
  docker compose stop csp ingestion-worker router anila-studio
  docker compose up -d --pull never csp-db
  local i
  for i in $(seq 1 30); do
    docker compose exec -T csp-db pg_isready -U csp -d postgres >/dev/null 2>&1 && break
    sleep 2
  done

  # 全新 volume 還原時 csp_app role 不存在 (cluster 級,不在 dump 內) → 先補建
  local app_pw; app_pw="$(get_env CSP_APP_DB_PASSWORD)"
  if ! docker compose exec -T csp-db psql -U csp -d postgres -tAc \
       "SELECT 1 FROM pg_roles WHERE rolname='csp_app'" 2>/dev/null | grep -q 1; then
    [ -n "$app_pw" ] || fatal "csp_app role 不存在且 .env 缺 CSP_APP_DB_PASSWORD — 無法補建"
    docker compose exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 -q \
      -c "CREATE ROLE csp_app LOGIN PASSWORD '$app_pw'"
    ok "已補建 csp_app role (密碼取自 .env)"
  fi

  # 舊庫「改名保留」而非 DROP:灌 dump 失敗還有路可退。
  local keep="csp_pre_restore_$(date +%Y%m%d%H%M%S)"
  log "把現有 csp 改名保留為 $keep,建新庫灌 dump"
  for i in $(seq 1 5); do
    docker compose exec -T csp-db psql -U csp -d postgres -q -c \
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='csp' AND pid <> pg_backend_pid()" >/dev/null
    docker compose exec -T csp-db psql -U csp -d postgres -q \
      -c "ALTER DATABASE csp RENAME TO $keep" 2>/dev/null && break
    [ "$i" = 5 ] && fatal "csp 改名失敗 (仍有連線佔用?) — 未動任何資料,已中止"
    sleep 2
  done
  docker compose exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE csp OWNER csp"
  if gunzip -c "$src/db-csp.sql.gz" | docker compose exec -T csp-db psql -U csp -d csp -v ON_ERROR_STOP=1 -q; then
    ok "DB 還原完成 (舊庫保留為 $keep,確認新庫沒問題後可清除)"
  else
    err "灌 dump 失敗 — 自動回滾到還原前狀態"
    docker compose exec -T csp-db psql -U csp -d postgres -q -c "DROP DATABASE IF EXISTS csp" || true
    docker compose exec -T csp-db psql -U csp -d postgres -v ON_ERROR_STOP=1 -q -c "ALTER DATABASE $keep RENAME TO csp"
    docker compose up -d --pull never csp ingestion-worker router anila-studio
    fatal "已回滾 (原資料完好)。檢查備份檔後再試"
  fi

  log "只重啟本次停止的 DB consumers，再跑全 stack fail-closed acceptance"
  docker compose up -d --pull never csp ingestion-worker router anila-studio
  bash infra/deployment/scripts/deploy-prod.sh wait
  bash infra/deployment/scripts/deploy-prod.sh verify
  warn "確認一切正常後清掉保留的舊庫:"
  warn "  docker compose exec -T csp-db psql -U csp -d postgres -c 'DROP DATABASE $keep'"
  warn "本指令目前只還原 DB；.env / secrets / uploads / attachments 尚未自動還原。"
  warn "secrets 與 runtime 目錄是 UID 10001 + mode 0700，禁止直接 cp/tar 或放寬權限。"
  warn "請用受控 root helper 還原並重設 owner/mode；Gate 6 restore drill 前須完成自動化 restore。"
  log "還原完成 — 用 'anila-ops.sh health' 驗一輪,再實際登入測一次"
}

# ── cert-renew (平台入向 TLS) ──────────────────────────────────────────────
cmd_cert_renew() {
  need_stack
  local pfx="${1:-}" pw
  [ -z "${2:-}" ] || fatal "PFX 密碼不可放命令列；腳本會安全提示輸入"
  [ -n "$pfx" ] && [ -f "$pfx" ] && [ ! -L "$pfx" ] || fatal "usage: anila-ops.sh cert-renew <server.pfx>"
  command -v openssl >/dev/null || fatal "找不到 openssl"
  pfx="$(realpath -e -- "$pfx")"
  case "$pfx" in "$REPO_ROOT"|"$REPO_ROOT"/*) fatal "PFX 不得放在 repo 內" ;; esac
  read -rsp "  PFX 密碼 (空密碼直接 Enter): " pw; echo

  local certs="$TLS_CERTS_DIR" archive_root="$ANILA_STATE_DIR/tls-archive" archive stamp staging new_crt new_key
  stamp="$(date +%Y%m%d-%H%M%S)"
  archive="$archive_root/$stamp"
  staging="$(mktemp -d "$certs/.renew.XXXXXX")" || fatal "無法建立 TLS staging directory"
  trap 'rm -rf -- "${staging:-}"' EXIT
  new_crt="$staging/server.crt"; new_key="$staging/server.key"

  # fullchain = leaf + pfx 內所有中繼 CA;sed 只留 PEM 區塊 (同 intranet-deploy [1/7])
  {
    openssl pkcs12 -in "$pfx" -clcerts -nokeys -legacy -passin fd:3 3<<<"$pw" 2>/dev/null
    openssl pkcs12 -in "$pfx" -cacerts -nokeys -legacy -passin fd:3 3<<<"$pw" 2>/dev/null
  } | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' > "$new_crt"
  grep -q 'BEGIN CERTIFICATE' "$new_crt" || fatal "抽 cert 失敗 (pfx 路徑/密碼?)"
  openssl pkcs12 -in "$pfx" -nocerts -noenc -legacy -passin fd:3 3<<<"$pw" 2>/dev/null \
    | openssl pkey > "$new_key" || fatal "抽 key 失敗"
  unset pw
  chmod 600 "$new_key"; chmod 644 "$new_crt"

  # key 與 cert 必須成對,錯配會讓 nginx 起不來
  local m1 m2
  m1="$(openssl x509 -in "$new_crt" -noout -pubkey 2>/dev/null | sha256sum | cut -d' ' -f1)"
  m2="$(openssl pkey -in "$new_key" -pubout 2>/dev/null | sha256sum | cut -d' ' -f1)"
  [ -n "$m1" ] && [ "$m1" = "$m2" ] || fatal "新 cert 與 key 不成對 — 已中止 (舊檔未動)"
  openssl x509 -in "$new_crt" -noout -checkend 86400 >/dev/null 2>&1 \
    || fatal "新 TLS 憑證已過期或 24 小時內到期"
  local tls_host
  for tls_host in anila.ai.ncsist.org.tw n8n.ai.ncsist.org.tw gitlab.ai.ncsist.org.tw code.ai.ncsist.org.tw; do
    openssl x509 -in "$new_crt" -noout -checkhost "$tls_host" >/dev/null 2>&1 \
      || fatal "新 TLS 憑證不涵蓋 $tls_host"
  done

  mkdir -p "$archive"
  chmod 700 "$archive_root" "$archive"
  [ -f "$certs/server.crt" ] && cp "$certs/server.crt" "$archive/"
  [ -f "$certs/server.key" ] && cp "$certs/server.key" "$archive/" \
    && chmod 600 "$archive/server.key"
  install -m 600 "$new_key" "$certs/server.key.next"
  install -m 644 "$new_crt" "$certs/server.crt.next"
  if ! mv -f -- "$certs/server.key.next" "$certs/server.key" || \
     ! mv -f -- "$certs/server.crt.next" "$certs/server.crt"; then
    [ -f "$archive/server.key" ] && cp "$archive/server.key" "$certs/server.key" || rm -f -- "$certs/server.key"
    [ -f "$archive/server.crt" ] && cp "$archive/server.crt" "$certs/server.crt" || rm -f -- "$certs/server.crt"
    fatal "TLS pair install 失敗，已回復舊檔"
  fi
  rm -rf -- "$staging"; staging=""; trap - EXIT
  ok "新憑證: $(openssl x509 -in "$certs/server.crt" -noout -subject -enddate | tr '\n' ' ')"

  # certs 目錄是 bind-mount,檔案換了只要 nginx reload。順序關鍵:nginx 在 reload
  # 前仍用「已載入」的舊憑證服務,所以先 `nginx -t` 驗新檔;被拒 → 自動把 archive
  # 的舊檔搬回來,全程零中斷、不會留下起不來的 nginx。
  if docker compose exec -T nginx nginx -t >/dev/null 2>&1; then
    docker compose exec -T nginx nginx -s reload \
      && ok "nginx 已 reload (零中斷)" \
      || { warn "reload 失敗,改 recreate nginx"; docker compose up -d --pull never --force-recreate nginx; }
  else
    err "nginx -t 拒絕新憑證 — 自動還原舊憑證"
    if [ -f "$archive/server.crt" ] && [ -f "$archive/server.key" ]; then
      cp "$archive/server.crt" "$certs/server.crt"
      cp "$archive/server.key" "$certs/server.key"
      chmod 600 "$certs/server.key"
      docker compose exec -T nginx nginx -t >/dev/null 2>&1 || warn "舊憑證也沒過 -t?手動檢查 $certs"
      fatal "已還原舊憑證 (服務未中斷)。檢查 pfx 內容 (fullchain/格式) 後再試"
    fi
    fatal "沒有舊憑證可還原 (首次部署?) — 檢查 pfx 後重跑,或用 intranet-deploy.sh [1/7]"
  fi
  log "TLS 換發完成 — 瀏覽器開 https://$(get_env ANILA_HOST || echo localhost)/ 確認無憑證警告"
  warn "舊憑證已備份到 $archive/（不在 nginx TLS mount 內）"
}

# ── model-ca (出向模型 gateway CA) ─────────────────────────────────────────
cmd_model_ca() {
  need_stack
  local pem="${1:-}"
  [ -n "$pem" ] && [ -s "$pem" ] || fatal "usage: anila-ops.sh model-ca <ca-chain.pem>"
  grep -q 'BEGIN CERTIFICATE' "$pem" || fatal "$pem 不是 PEM 憑證"

  # 先在 host 驗信任鏈成立再交給容器 (verify return code: 0 才算數)。
  # SSL_CERT_FILE 是「取代」整個信任庫:壞檔會讓 csp 所有出向 https 全倒。
  local base host
  base="$(get_env LOCAL_LLM_BASE_URL)"
  if [ -n "$base" ]; then
    host="${base#https://}"; host="${host%%/*}"
    if echo | openssl s_client -connect "$host:443" -servername "$host" -CAfile "$pem" 2>/dev/null \
        | grep -q 'Verify return code: 0'; then
      ok "host 端驗證通過: $host 信任鏈完整 (return code 0)"
    else
      warn "用 $pem 驗 $host 失敗 — 換上去 csp 會 CERTIFICATE_VERIFY_FAILED"
      local c; read -rp "  仍要繼續? [y/N]: " c; [ "$c" = y ] || fatal "已取消"
    fi
  fi
  mkdir -p share/pki
  cp "$pem" share/pki/model-ca.pem
  [ "$(get_env ANILA_MODEL_CA_FILE)" = /etc/anila/pki/model-ca.pem ] \
    || set_env ANILA_MODEL_CA_FILE /etc/anila/pki/model-ca.pem
  # 掛載檔內容變更 → 必須 recreate 讓 csp 重讀 (restart 不重載 .env)
  docker compose up -d --pull never --force-recreate csp
  log "model-ca 已更新並 recreate csp — 用 'anila-ops.sh health' 驗模型鏈路"
}

# ── gateway-key ────────────────────────────────────────────────────────────
cmd_gateway_key() {
  need_stack
  [ -f .env ] || fatal ".env 不存在"
  local key
  read -rsp "  新的 MODEL_GATEWAY_API_KEY (在 .12 gateway 簽發): " key; echo
  [ -n "$key" ] || fatal "key 不能空"
  set_env_single_quoted MODEL_GATEWAY_API_KEY "$key"
  # env 變更 → up -d 會自動 recreate csp (compose 偵測 env diff)
  docker compose up -d --pull never csp
  log "csp 已以新 key recreate,探測 gateway..."
  health_model_gateway
  [ "$_FAILS" -eq 0 ] && log "gateway key 輪替完成" \
    || { err "探測未過 — 確認 key 在 .12 有效、防火牆/DNS 正常"; exit 1; }
}

# ── break-glass ────────────────────────────────────────────────────────────
cmd_break_glass() {
  need_stack
  [ -f .env ] || fatal ".env 不存在"
  case "${1:-}" in
    on)
      warn "break-glass = 暫時開放帳密登入 (card-only 關閉)。只在讀卡機 / HiPKI 全面故障時使用!"
      local c; read -rp "  確定開啟? [y/N]: " c; [ "$c" = y ] || fatal "已取消"
      set_env REQUIRE_CARD_LOGIN_ONLY false
      docker compose up -d --pull never csp
      log "已開啟帳密後路 — 用 owner 帳密 (admin / .env 的 ADMIN_PASSWORD) 登入處理"
      warn "修好讀卡環境後務必跑: anila-ops.sh break-glass off"
      ;;
    off)
      set_env REQUIRE_CARD_LOGIN_ONLY true
      docker compose up -d --pull never csp
      log "已恢復 card-only 模式 (REQUIRE_CARD_LOGIN_ONLY=true)"
      ;;
    *) fatal "usage: anila-ops.sh break-glass on|off" ;;
  esac
}

# ── prune ──────────────────────────────────────────────────────────────────
cmd_prune() {
  need_stack
  section "目前 docker 空間"
  docker system df
  local c; read -rp "  清除 dangling image + builder cache?(不碰 volume / 使用中 image) [y/N]: " c
  [ "$c" = y ] || fatal "已取消"
  docker image prune -f
  docker builder prune -f
  log "prune 完成"
}

# ── help ───────────────────────────────────────────────────────────────────
cmd_help() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

# ── entrypoint ─────────────────────────────────────────────────────────────
SUBCMD="${1:-help}"; shift || true
case "$SUBCMD" in
  status)      cmd_status ;;
  health)      cmd_health ;;
  logs)        cmd_logs "$@" ;;
  backup)      cmd_backup "$@" ;;
  restore)     cmd_restore "$@" ;;
  cert-renew)  cmd_cert_renew "$@" ;;
  model-ca)    cmd_model_ca "$@" ;;
  gateway-key) cmd_gateway_key ;;
  break-glass) cmd_break_glass "$@" ;;
  prune)       cmd_prune ;;
  help|-h|--help) cmd_help ;;
  *) err "未知 subcommand: $SUBCMD"; cmd_help; exit 1 ;;
esac
