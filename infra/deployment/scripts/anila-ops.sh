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
#   backup                     加密、簽章並發佈 production backup bundle
#   restore <bundle> <new-target> [prepare|smoke]
#                              只還原到全新可拋棄目標；不支援 live/in-place restore
#   cert-renew <pfx>           平台 TLS 換發:安全提示密碼後重抽 fullchain+key → nginx reload
#   model-ca <pem>             更新模型 gateway 出向 CA → 重建 csp
#   gateway-key                輪替 MODEL_GATEWAY_API_KEY → 重建 csp → 探測
#   break-glass on|off [...]   具名、限時 owner 帳密應急 / 恢復純卡片姿態
#   prune                      清 dangling image + builder cache (不碰 volume)
#   help                       顯示這份說明
#
# 鐵則 (寫死在本腳本的行為,不要繞過):
#   1. 套用 .env / 掛載檔變更一律 `docker compose up -d --no-build --pull never [--force-recreate]`,
#      絕不用 `docker restart` (不重載 env,見 AGENTS.md §4)。
#   2. 本腳本不動 ANILA_ALLOW_* / ANILA_ENV 等 strict 旗標。break-glass
#      只切換已審查的 deployment profile；REQUIRE_CARD_LOGIN_ONLY 始終為 true。
#   3. DB/state 只會直接串流進 age；JWT/TLS private key 只備份外部 reference
#      與 public fingerprint。backup/off-host 目錄必須位於 repo 外。
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
get_env_unquoted() {
  local value
  value="$(get_env "$1")"
  case "$value" in
    \'*\'|\"*\") value="${value:1:${#value}-2}" ;;
  esac
  printf '%s' "$value"
}
IMAGE_LOCK_VERIFIER="$REPO_ROOT/infra/deployment/scripts/verify-compose-image-lock.py"

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

# 備份含加密後的全庫 dump 與 classified state。即使 operator 覆寫路徑，
# 也不可重新落進 repo (code-server / IDE / accidental archive 的可見範圍)。
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
  printf -v "$key" '%s' "$val"
  export "$key"
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
  printf -v "$key" '%s' "$val"
  export "$key"
}

unset_env() {
  local key="$1" tmp
  [ -f .env ] && [ ! -L .env ] || fatal ".env 必須是 regular file 且不得是 symlink"
  tmp="$(mktemp "$REPO_ROOT/.env.tmp.XXXXXX")" || fatal "無法建立安全 .env temp file"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  chmod 600 "$tmp"
  mv -f -- "$tmp" .env
  unset "$key"
}

csp_env_readback() {
  local key="$1"
  docker compose exec -T csp python -c \
    'import os,sys; print(os.environ.get(sys.argv[1], ""))' "$key" 2>/dev/null \
    || fatal "無法從 running CSP 回讀 $key"
}

need_stack() {
  command -v docker >/dev/null || fatal "找不到 docker"
  docker info >/dev/null 2>&1  || fatal "docker daemon 沒在跑 / 當前使用者無權限"
  [ -f compose.yaml ]          || fatal "請在 repo 根目錄執行 (找不到 compose.yaml)"
  local compose_variable
  for compose_variable in COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME \
    COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PATH_SEPARATOR; do
    [[ ! -v "$compose_variable" ]] \
      || fatal "formal lifecycle 不接受 ambient $compose_variable；請 unset 後重跑"
  done
  local managed_variable file_value
  for managed_variable in \
    ANILA_DEPLOYMENT_PROFILE ANILA_ENV ANILA_ALLOW_DEV_SECRET DEBUG \
    ENABLE_API_DOCS ENABLE_PUBLIC_SHARE ENABLE_MEMORY SKIP_STARTUP_MIGRATIONS \
    ALLOW_AUTO_KEYGEN COOKIE_SECURE ENABLE_CARD_LOGIN REQUIRE_CARD_LOGIN_ONLY \
    ANILA_ALLOW_HTTP_ENDPOINT ANILA_ALLOW_HTTP_AGENT_ENDPOINT \
    ANILA_ALLOW_PRIVATE_ENDPOINT CARD_DEV_SKIP_NONCE_BINDING \
    CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CSP_SECRET_KEY CSP_SERVICE_TOKEN \
    STUDIO_ARTIFACT_SERVICE_TOKEN \
    INTERNAL_PLATFORM_API_KEY ADMIN_PASSWORD MODEL_GATEWAY_API_KEY \
    N8N_ENCRYPTION_KEY GITLAB_ROOT_PASSWORD ANILA_STATE_DIR \
    ANILA_SECRETS_DIR ANILA_TLS_CERTS_DIR ANILA_BREAK_GLASS_OWNER \
    ANILA_BREAK_GLASS_TICKET ANILA_BREAK_GLASS_EXPIRES_AT; do
    if [[ -v "$managed_variable" ]]; then
      file_value="$(get_env_unquoted "$managed_variable")"
      [[ "${!managed_variable}" == "$file_value" ]] \
        || fatal "ambient $managed_variable 與 .env 不一致；請 unset 或重新載入正式 .env"
    fi
  done
  case "$(get_env ANILA_DEPLOYMENT_PROFILE)" in
    prod-intranet-card|prod-intranet-card-breakglass)
      command -v python3 >/dev/null || fatal "formal image-lock 驗證需要 python3"
      python3 "$IMAGE_LOCK_VERIFIER" verify-env --env-file .env --inspect-docker \
        || fatal "formal Compose image content-ID lock 驗證失敗"
      ;;
    *)
      fatal "anila-ops.sh 本版只允許已審查的 prod-intranet-card profile"
      ;;
  esac
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

# ── backup / restore ───────────────────────────────────────────────────────
# Legacy plaintext backup and live/in-place restore were removed at Gate 3 I9.
# Only the fail-closed production backup wrapper near the entrypoint is callable.

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
      || { warn "reload 失敗,改 recreate nginx"; docker compose up -d --no-build --pull never --force-recreate nginx; }
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
  docker compose up -d --no-build --pull never --force-recreate csp
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
  docker compose up -d --no-build --pull never csp
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
      local owner="${2:-}" ticket="${3:-}" hours="${4:-}" expires_at c
      warn "break-glass = 在 card-only 姿態中，暫時只允許 owner 帳密。只在讀卡機 / HiPKI 全面故障時使用!"
      read -rp "  確定開啟? [y/N]: " c; [ "$c" = y ] || fatal "已取消"
      [ -n "$owner" ] || read -rp "  具名系統負責人: " owner
      [ -n "$ticket" ] || read -rp "  Incident/ticket 編號: " ticket
      [ -n "$hours" ] || read -rp "  有效小時 (1-24，建議 1): " hours
      hours="${hours:-1}"
      [[ "$owner" =~ ^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,127}$ ]] \
        || fatal "負責人必須是 3-128 字元 audit identifier"
      [[ "$ticket" =~ ^[A-Za-z0-9][A-Za-z0-9._:@/-]{2,127}$ ]] \
        || fatal "ticket 必須是 3-128 字元 audit identifier"
      [[ "$hours" =~ ^[0-9]+$ ]] && (( hours >= 1 && hours <= 24 )) \
        || fatal "有效小時必須是 1-24 的整數"
      expires_at="$(date -u -d "+${hours} hours" '+%Y-%m-%dT%H:%M:%SZ')" \
        || fatal "無法計算 UTC expiry"
      set_env REQUIRE_CARD_LOGIN_ONLY true
      set_env_single_quoted ANILA_BREAK_GLASS_OWNER "$owner"
      set_env_single_quoted ANILA_BREAK_GLASS_TICKET "$ticket"
      set_env ANILA_BREAK_GLASS_EXPIRES_AT "$expires_at"
      set_env ANILA_DEPLOYMENT_PROFILE prod-intranet-card-breakglass
      docker compose up -d --no-build --pull never csp
      [ "$(csp_env_readback ANILA_DEPLOYMENT_PROFILE)" = prod-intranet-card-breakglass ] \
        || fatal "CSP effective profile 未切入 break-glass（可能有 ambient env override）"
      [ "$(csp_env_readback REQUIRE_CARD_LOGIN_ONLY)" = true ] \
        || fatal "CSP effective card-only flag 漂移"
      [ "$(csp_env_readback ANILA_BREAK_GLASS_TICKET)" = "$ticket" ] \
        || fatal "CSP break-glass ticket read-back 不一致"
      log "已開啟具名 owner 帳密後路；owner=$owner ticket=$ticket expires=$expires_at"
      warn "修好讀卡環境後務必跑: anila-ops.sh break-glass off"
      ;;
    off)
      set_env REQUIRE_CARD_LOGIN_ONLY true
      set_env ANILA_DEPLOYMENT_PROFILE prod-intranet-card
      unset_env ANILA_BREAK_GLASS_OWNER
      unset_env ANILA_BREAK_GLASS_TICKET
      unset_env ANILA_BREAK_GLASS_EXPIRES_AT
      docker compose up -d --no-build --pull never csp
      [ "$(csp_env_readback ANILA_DEPLOYMENT_PROFILE)" = prod-intranet-card ] \
        || fatal "CSP effective profile 未恢復 prod-intranet-card"
      [ "$(csp_env_readback REQUIRE_CARD_LOGIN_ONLY)" = true ] \
        || fatal "CSP effective card-only flag 未恢復"
      log "已恢復 prod-intranet-card 純卡片姿態，並清除 break-glass metadata"
      ;;
    *) fatal "usage: anila-ops.sh break-glass on [owner ticket hours]|off" ;;
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
# Gate 3 I9 production path. Do not reintroduce plaintext backup or destructive
# live restore into this daily operator command.
PRODUCTION_BACKUP_TOOL="$REPO_ROOT/infra/deployment/scripts/production-backup.py"

cmd_backup() {
  [ -f "$PRODUCTION_BACKUP_TOOL" ] \
    || fatal "缺少 production backup tool: $PRODUCTION_BACKUP_TOOL"
  [ "$#" -eq 0 ] || fatal "usage: anila-ops.sh backup"
  exec python3 "$PRODUCTION_BACKUP_TOOL" \
    --repo-root "$REPO_ROOT" backup --backup-root "$BACKUP_DIR"
}

cmd_restore() {
  [ -f "$PRODUCTION_BACKUP_TOOL" ] \
    || fatal "缺少 production restore tool: $PRODUCTION_BACKUP_TOOL"
  local bundle="${1:-}" target="${2:-}" mode="${3:-prepare}"
  [ -n "$bundle" ] && [ -n "$target" ] \
    || fatal "usage: anila-ops.sh restore <signed-bundle-dir> <new-disposable-target> [prepare|smoke]"
  [ "$#" -le 3 ] || fatal "restore 參數過多"
  case "$mode" in
    prepare)
      exec python3 "$PRODUCTION_BACKUP_TOOL" --repo-root "$REPO_ROOT" \
        restore-prepare "$bundle" --target "$target"
      ;;
    smoke)
      exec python3 "$PRODUCTION_BACKUP_TOOL" --repo-root "$REPO_ROOT" \
        restore-smoke "$bundle" --target "$target"
      ;;
    *) fatal "restore mode 只能是 prepare 或 smoke；正式 destructive restore 留在 Gate 6" ;;
  esac
}

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
