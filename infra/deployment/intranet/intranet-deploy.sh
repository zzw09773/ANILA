#!/usr/bin/env bash
# ============================================================================
# intranet-deploy.sh — 內網一條龍部署 (prod-intranet-card / V1.0.0 卡片登入)
# ----------------------------------------------------------------------------
# 在內網平台主機 (.15) 上,一支互動式腳本跑完:
#   [1] TLS 憑證抽取 (從 server.pfx)
#   [2] 模型 gateway 出向 CA (share/pki/model-ca.pem)
#   [3] 產 / 更新 .env (自動生 secret + 互動填 gateway key / owner 員工編號)
#   [4] load image (呼叫 image 包的 INTRANET-LOAD.sh,含 SHA256 驗檔 + re-tag)
#   [5] 建 docker network
#   [6] docker compose up -d --no-build --pull never
#   [7] 等 healthy + 驗證
#
# 用法 (在 prod-intranet-card repo 根目錄):
#   bash infra/deployment/intranet/intranet-deploy.sh [IMAGE_BUNDLE_DIR]
#   IMAGE_BUNDLE_DIR 預設自動找 ./intranet-prod-v1.0.0;找不到會提示輸入。
#
# 重跑安全:偵測到既有 .env 預設「保留現有 secret」— 避免重生 DB 密碼炸掉既有
# DB。只有你明確選擇重生才會換 secret(並先備份舊 .env)。
#
# 注意:這支不碰 model image / 權重(第一版走 .12 gateway)。後續日常操作用
# infra/deployment/scripts/deploy-prod.sh {status|logs|restart|down}。
# ============================================================================
set -euo pipefail
umask 077

c()    { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info() { echo "$(c '1;36' '▶') $*"; }
ok()   { echo "$(c '1;32' '✓') $*"; }
warn() { echo "$(c '1;33' '⚠') $*"; }
die()  { echo "$(c '1;31' '✗') $*" >&2; exit 1; }
ask()       { local p="$1" d="${2:-}" a; read -rp "$(c '1;35' '?') ${p}${d:+ [$d]}: " a; printf '%s' "${a:-$d}"; }
asksecret() { local p="$1" a; read -rsp "$(c '1;35' '?') ${p}: " a; echo >&2; printf '%s' "$a"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

# Must match infra/docker/csp.Dockerfile.  A fixed numeric identity makes bind
# mount ownership deterministic even though the image is deployed offline.
CSP_RUNTIME_UID=10001
CSP_RUNTIME_GID=10001

read_existing_env() {
  local value
  value="$(grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
  if [[ "$value" == \'*\' ]] || [[ "$value" == \"*\" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

if [ -n "${XDG_STATE_HOME:-}" ]; then
  DEFAULT_STATE_DIR="$XDG_STATE_HOME/anila"
elif [ -n "${HOME:-}" ]; then
  DEFAULT_STATE_DIR="$HOME/.local/state/anila"
else
  DEFAULT_STATE_DIR="/var/lib/anila"
fi
_shell_state="${ANILA_STATE_DIR:-}"
_shell_secrets="${ANILA_SECRETS_DIR:-}"
_shell_tls="${ANILA_TLS_CERTS_DIR:-}"
_existing_state="$(read_existing_env ANILA_STATE_DIR)"
_existing_secrets="$(read_existing_env ANILA_SECRETS_DIR)"
_existing_tls="$(read_existing_env ANILA_TLS_CERTS_DIR)"
for _pair in \
  "ANILA_STATE_DIR|$_shell_state|$_existing_state" \
  "ANILA_SECRETS_DIR|$_shell_secrets|$_existing_secrets" \
  "ANILA_TLS_CERTS_DIR|$_shell_tls|$_existing_tls"; do
  _label="${_pair%%|*}"; _rest="${_pair#*|}"
  _shell_value="${_rest%%|*}"; _saved_value="${_rest#*|}"
  if [ -n "$_shell_value" ] && [ -n "$_saved_value" ] && [ "$_shell_value" != "$_saved_value" ]; then
    die "$_label 的 shell 值與既有 .env 不一致；私鑰路徑變更必須走明確 rotation"
  fi
done
if [ -z "$_existing_state" ] && [ -n "$_existing_secrets" ] && [ -n "$_existing_tls" ]; then
  if [ "$(dirname -- "$_existing_secrets")" = "$(dirname -- "$_existing_tls")" ]; then
    _existing_state="$(dirname -- "$_existing_secrets")"
  else
    die "既有 ANILA_SECRETS_DIR / ANILA_TLS_CERTS_DIR 不在同一受控 state root"
  fi
fi
ANILA_STATE_DIR="${_shell_state:-${_existing_state:-$DEFAULT_STATE_DIR}}"
ENV_BACKUP_DIR="${ANILA_ENV_BACKUP_DIR:-$ANILA_STATE_DIR/env-backups}"
SECRETS_DIR="${_shell_secrets:-${_existing_secrets:-$ANILA_STATE_DIR/secrets}}"
TLS_CERTS_DIR="${_shell_tls:-${_existing_tls:-$ANILA_STATE_DIR/tls}}"

assert_outside_repo() {
  local raw="$1" label="$2" repo_real target_real
  repo_real="$(realpath -m -- "$REPO_ROOT")"
  target_real="$(realpath -m -- "$raw")"
  case "$target_real" in
    "$repo_real"|"$repo_real"/*)
      die "$label 不得位於 repo 內: $target_real (請設到獨立受控磁碟/目錄)" ;;
  esac
  OUTSIDE_PATH="$target_real"
}

assert_safe_state_layout() {
  local state_lex state_real target_lex target_real label raw leaf
  [[ "$ANILA_STATE_DIR" = /* ]] || die "ANILA_STATE_DIR 必須是絕對路徑"
  state_lex="$(realpath -ms -- "$ANILA_STATE_DIR")"
  state_real="$(realpath -m -- "$ANILA_STATE_DIR")"
  [[ "$state_lex" == "$state_real" ]] \
    || die "ANILA_STATE_DIR 不得包含 symlink component: $ANILA_STATE_DIR"
  case "$state_real" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib)
      die "ANILA_STATE_DIR 太寬或是系統目錄: $state_real" ;;
  esac
  assert_outside_repo "$state_real" ANILA_STATE_DIR
  ANILA_STATE_DIR="$state_real"
  for label in ANILA_SECRETS_DIR ANILA_TLS_CERTS_DIR; do
    if [ "$label" = ANILA_SECRETS_DIR ]; then
      raw="$SECRETS_DIR"; leaf=secrets
    else
      raw="$TLS_CERTS_DIR"; leaf=tls
    fi
    [[ "$raw" = /* ]] || die "$label 必須是絕對路徑"
    target_lex="$(realpath -ms -- "$raw")"
    target_real="$(realpath -m -- "$raw")"
    [[ "$target_lex" == "$target_real" ]] \
      || die "$label 不得包含 symlink component: $raw"
    [[ "$target_real" == "$state_real/$leaf" ]] \
      || die "$label 必須固定為 $state_real/$leaf (目前: $target_real)"
    if [ "$label" = ANILA_SECRETS_DIR ]; then
      SECRETS_DIR="$target_real"
    else
      TLS_CERTS_DIR="$target_real"
    fi
  done
}

set_env() {  # set_env KEY VALUE — 完整寫入 temp 後 atomic rename
  local key="$1" val="$2" tmp
  [ -f .env ] && [ ! -L .env ] || die ".env 必須是 regular file 且不得是 symlink"
  tmp="$(mktemp "$REPO_ROOT/.env.tmp.XXXXXX")" || die "無法建立安全 .env temp file"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv -f -- "$tmp" .env
}
get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }
get_env_unquoted() {
  local value
  value="$(get_env "$1")"
  if [[ "$value" == \'*\' ]] || [[ "$value" == \"*\" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}
set_env_single_quoted() {
  local key="$1" val="$2" tmp
  [[ "$val" != *"'"* && "$val" != *$'\n'* && "$val" != *$'\r'* ]] \
    || die "$key 含不能安全寫入 .env 單引號值的字元"
  [ -f .env ] && [ ! -L .env ] || die ".env 必須是 regular file 且不得是 symlink"
  tmp="$(mktemp "$REPO_ROOT/.env.tmp.XXXXXX")" || die "無法建立安全 .env temp file"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  printf "%s='%s'\n" "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv -f -- "$tmp" .env
}
unset_env() {
  local key="$1" tmp
  [ -f .env ] || return 0
  [ ! -L .env ] || die ".env 不得是 symlink"
  tmp="$(mktemp "$REPO_ROOT/.env.tmp.XXXXXX")" || die "無法建立安全 .env temp file"
  grep -vE "^${key}=" .env > "$tmp" 2>/dev/null || true
  chmod 600 "$tmp"
  mv -f -- "$tmp" .env
}
backup_existing_env() {
  mkdir -p "$ENV_BACKUP_DIR" || die "無法建立 .env 備份目錄: $ENV_BACKUP_DIR"
  chmod 700 "$ENV_BACKUP_DIR"
  LAST_ENV_BACKUP="$ENV_BACKUP_DIR/anila.env.$(date +%Y%m%d-%H%M%S).bak"
  ( umask 077; cp -- .env "$LAST_ENV_BACKUP" ) \
    || die "備份既有 .env 失敗: $LAST_ENV_BACKUP"
  chmod 600 "$LAST_ENV_BACKUP"
}

prepare_csp_runtime_mount() {
  local host_path="$1" dir_mode="$2"
  mkdir -p "$host_path"
  # Image must already have been loaded by INTRANET-LOAD.sh.  Never let this
  # permission helper fall back to a registry pull on the air-gapped host.
  docker run --rm --pull never --user 0:0 --network none --read-only \
    --security-opt no-new-privileges \
    -v "$host_path:/mnt" \
    --entrypoint sh "$(get_env ANILA_IMAGE_CSP)" \
    -c "chown $CSP_RUNTIME_UID:$CSP_RUNTIME_GID /mnt && chmod $dir_mode /mnt" \
    || die "無法準備 CSP non-root mount: $host_path"
}

echo "============================================================"
echo " ANILA 內網一條龍部署 — V1.0.0 (prod-intranet-card / 卡片登入)"
echo "============================================================"

# ── 0. 前置檢查 ───────────────────────────────────────────────────────────
info "[0/7] 前置檢查"
command -v docker >/dev/null   || die "找不到 docker"
command -v openssl >/dev/null  || die "找不到 openssl"
command -v realpath >/dev/null || die "找不到 realpath (無法驗證 secret 備份路徑)"
docker info >/dev/null 2>&1    || die "docker daemon 沒在跑 / 當前使用者無權限"
[ -f compose.yaml ] && [ -f .env.example ] \
  || die "請在 prod-intranet-card repo 根目錄執行(找不到 compose.yaml / .env.example)"
br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
[ "$br" = prod-intranet-card ] || die "目前 git 分支是 '$br'；一條龍正式部署只允許 prod-intranet-card"
OUTSIDE_PATH=""
assert_safe_state_layout
assert_outside_repo "$ENV_BACKUP_DIR" ANILA_ENV_BACKUP_DIR
ENV_BACKUP_DIR="$OUTSIDE_PATH"
export ANILA_STATE_DIR
export ANILA_SECRETS_DIR="$SECRETS_DIR"
export ANILA_TLS_CERTS_DIR="$TLS_CERTS_DIR"

BUNDLE="${1:-}"
if [ -z "$BUNDLE" ]; then
  for cand in ./intranet-prod-v1.0.0 ../intranet-prod-v1.0.0 ./intranet-images-export /tmp/anila-images-export; do
    [ -f "$cand/INTRANET-LOAD.sh" ] && { BUNDLE="$cand"; break; }
  done
fi
[ -n "$BUNDLE" ] || BUNDLE="$(ask 'image 包資料夾路徑 (含 INTRANET-LOAD.sh)')"
[ -f "$BUNDLE/INTRANET-LOAD.sh" ] || die "在 '$BUNDLE' 找不到 INTRANET-LOAD.sh"
BUNDLE="$(cd "$BUNDLE" && pwd)"
ok "image 包: $BUNDLE"

# ── 1. TLS 憑證 ───────────────────────────────────────────────────────────
info "[1/7] TLS 憑證 (wildcard *.ai.ncsist.org.tw)"
mkdir -p "$TLS_CERTS_DIR"
chmod 700 "$TLS_CERTS_DIR"
CRT="$TLS_CERTS_DIR/server.crt"
KEY="$TLS_CERTS_DIR/server.key"
DO_TLS=1
if [ -f "$CRT" ] && [ -f "$KEY" ]; then
  warn "已有 server.crt / server.key"
  [ "$(ask '重新從 pfx 抽取?(會覆蓋) [y/N]' N)" = y ] || DO_TLS=0
fi
if [ "$DO_TLS" = 1 ]; then
  PFX="$(ask 'server.pfx 路徑')"; [ -f "$PFX" ] && [ ! -L "$PFX" ] || die "PFX 必須是 regular non-symlink file"
  PFX="$(realpath -e -- "$PFX")"
  case "$PFX" in "$REPO_ROOT"|"$REPO_ROOT"/*) die "PFX 不得放在 repo 內" ;; esac
  PFXPW="$(asksecret 'pfx 密碼 (空就直接 Enter)')"
  TLS_TMP="$(mktemp -d "$TLS_CERTS_DIR/.extract.XXXXXX")" || die "無法建立 TLS staging directory"
  trap 'rm -rf -- "${TLS_TMP:-}"' EXIT
  NEW_CRT="$TLS_TMP/server.crt"
  NEW_KEY="$TLS_TMP/server.key"
  # server.crt 抽 fullchain:leaf(client cert)在前 + pfx 內所有中繼 CA,瀏覽器才能
  # 驗完整鏈(只放 leaf 在缺中繼的環境會跳「憑證不受信任」)。sed 只留 PEM 區塊,
  # 去掉 openssl 的 Bag Attributes 雜訊。
  {
    openssl pkcs12 -in "$PFX" -clcerts -nokeys -legacy -passin fd:3 3<<<"$PFXPW" 2>/dev/null
    openssl pkcs12 -in "$PFX" -cacerts -nokeys -legacy -passin fd:3 3<<<"$PFXPW" 2>/dev/null
  } | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' > "$NEW_CRT"
  [ -s "$NEW_CRT" ] && grep -q 'BEGIN CERTIFICATE' "$NEW_CRT" || die "抽 server.crt 失敗(pfx 路徑/密碼?)"
  openssl pkcs12 -in "$PFX" -nocerts -noenc -legacy -passin fd:3 3<<<"$PFXPW" 2>/dev/null \
    | openssl pkey > "$NEW_KEY" || die "抽 server.key 失敗"
  unset PFXPW
  chmod 600 "$NEW_KEY"; chmod 644 "$NEW_CRT"
  _new_cert_pub="$(openssl x509 -in "$NEW_CRT" -noout -pubkey 2>/dev/null | sha256sum | cut -d' ' -f1)"
  _new_key_pub="$(openssl pkey -in "$NEW_KEY" -pubout 2>/dev/null | sha256sum | cut -d' ' -f1)"
  [ -n "$_new_cert_pub" ] && [ "$_new_cert_pub" = "$_new_key_pub" ] || die "PFX 內 cert 與 key 不成對"
  openssl x509 -in "$NEW_CRT" -noout -checkend 86400 >/dev/null 2>&1 \
    || die "新 TLS 憑證已過期或 24 小時內到期"
  for _tls_host in anila.ai.ncsist.org.tw n8n.ai.ncsist.org.tw \
                   gitlab.ai.ncsist.org.tw code.ai.ncsist.org.tw; do
    openssl x509 -in "$NEW_CRT" -noout -checkhost "$_tls_host" >/dev/null 2>&1 \
      || die "新 TLS 憑證不涵蓋 $_tls_host"
  done
  # nginx 只需讀 live pair；舊 private keys 必須放在 mount 之外，避免
  # nginx compromise 一次讀走所有歷史金鑰。
  _tls_archive="$ANILA_STATE_DIR/tls-archive/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$_tls_archive"
  [ -f "$CRT" ] && cp -- "$CRT" "$_tls_archive/server.crt"
  [ -f "$KEY" ] && { cp -- "$KEY" "$_tls_archive/server.key"; chmod 600 "$_tls_archive/server.key"; }
  install -m 600 "$NEW_KEY" "$TLS_CERTS_DIR/server.key.next"
  install -m 644 "$NEW_CRT" "$TLS_CERTS_DIR/server.crt.next"
  if ! mv -f -- "$TLS_CERTS_DIR/server.key.next" "$KEY" || \
     ! mv -f -- "$TLS_CERTS_DIR/server.crt.next" "$CRT"; then
    [ -f "$_tls_archive/server.key" ] && cp -- "$_tls_archive/server.key" "$KEY" || rm -f -- "$KEY"
    [ -f "$_tls_archive/server.crt" ] && cp -- "$_tls_archive/server.crt" "$CRT" || rm -f -- "$CRT"
    die "TLS pair atomic install 失敗，已回復舊檔"
  fi
  rm -rf -- "$TLS_TMP"; TLS_TMP=""; trap - EXIT
  _ncert=$(grep -c 'BEGIN CERTIFICATE' "$CRT")
  ok "TLS 憑證抽取完成(fullchain $_ncert 張:leaf + $((_ncert - 1)) 中繼)"
fi
[[ -f "$CRT" && ! -L "$CRT" && -f "$KEY" && ! -L "$KEY" ]] || die "TLS cert/key 必須是 regular non-symlink files"
openssl x509 -in "$CRT" -noout -checkend 86400 >/dev/null 2>&1 || die "TLS 憑證已過期或 24 小時內到期"
_cert_pub="$(openssl x509 -in "$CRT" -noout -pubkey 2>/dev/null | sha256sum | cut -d' ' -f1)"
_key_pub="$(openssl pkey -in "$KEY" -pubout 2>/dev/null | sha256sum | cut -d' ' -f1)"
[ -n "$_cert_pub" ] && [ "$_cert_pub" = "$_key_pub" ] || die "TLS cert 與 key 不成對"
echo "    $(openssl x509 -in "$CRT" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
for _tls_host in anila.ai.ncsist.org.tw n8n.ai.ncsist.org.tw \
                 gitlab.ai.ncsist.org.tw code.ai.ncsist.org.tw; do
  openssl x509 -in "$CRT" -noout -checkhost "$_tls_host" >/dev/null 2>&1 \
    || die "TLS 憑證不涵蓋 $_tls_host；不可用不匹配憑證啟動工具子網域"
done
ok "TLS SAN 涵蓋主平台與三個工具子網域"

# ── 2. 模型 gateway 出向 CA ───────────────────────────────────────────────
info "[2/7] 模型 gateway CA (對 https://aiagent2.ai.ncsist.org.tw 的出向 TLS 信任)"
# share/* 是 nginx(static)、csp + ingestion-worker(uploads)的 bind-mount 來源;
# 先建好,否則 docker 會以 root 自動建空目錄(權限/擁有者錯亂)。
mkdir -p share/pki share/static share/uploads/ingestion share/codeserver-sandbox
chmod 700 share/codeserver-sandbox
MCA=share/pki/model-ca.pem
# 內網模型 gateway 走 *.ai.ncsist.org.tw,憑證由中科院 CSPKI 簽發
# (CSPKI Root CA G1 → 中科院憑證管理中心 G1 → leaf)。中科院整套 PKI 同一條根:
# PKI 卡與內網伺服器憑證皆 CSPKI 簽。repo 內 cspki_ca_bundle.pem 是「卡片登入驗章」
# 釘死的同一套信任錨(Root + 中繼),正好就是這條鏈 → 直接當 model-ca,csp 即可
# 信任 .12 gateway 的 https。免下載、免跟 IT 要,離線就有。
CSPKI_BUNDLE=services/csp/app/services/cspki_ca_bundle.pem
if [ -f "$MCA" ]; then
  ok "已有 $MCA (沿用,不覆蓋)"
elif [ -f "$CSPKI_BUNDLE" ]; then
  cp "$CSPKI_BUNDLE" "$MCA"
  ok "model-ca.pem ← CSPKI bundle (Root CA G1 + 中科院憑證管理中心;內網 https 同一套 CA)"
else
  warn "找不到 $CSPKI_BUNDLE — 改由 IT 提供"
  P="$(ask 'model CA PEM 路徑 (IT 給的;留空則稍後手動放 share/pki/model-ca.pem)')"
  if [ -n "$P" ] && [ -f "$P" ]; then cp "$P" "$MCA"; ok "已複製 model-ca.pem"
  else warn "略過 — 部署前務必把 model CA 放到 $MCA,否則 csp 連 gateway 會 TLS 失敗"; fi
fi

# ── 3. .env ──────────────────────────────────────────────────────────────
info "[3/7] 產生 / 更新 .env"
REGEN=1
if [ -f .env ]; then
  warn "已有 .env"
  if [ "$(ask '保留現有 secret?(建議 Y — 重生 DB 密碼會炸掉既有 DB) [Y/n]' Y)" = n ]; then
    backup_existing_env
    warn "已備份舊 .env 到 $LAST_ENV_BACKUP;將重生 secret"
  else
    REGEN=0
  fi
else
  cp .env.example .env; ok "由 .env.example 建立 .env"
fi
chmod 600 .env

# 預設值來源(選用):image 包裡的 intranet-defaults.env(刻意不進 git,實體隨包帶入
# air-gap)。提供 ADMIN_PASSWORD / CODESERVER_PASSWORD / CARD_INITIAL_OWNERS /
# GITLAB_ROOT_PASSWORD 等;沒提供的 secret 一律 openssl 隨機生成。
# 安全:不用 `source`(被竄改的 defaults 檔會執行任意指令),改嚴格解析 KEY=VALUE
# + 白名單;非白名單 / 註解 / 空行一律略過。
DEFAULTS="$BUNDLE/intranet-defaults.env"
if [ -f "$DEFAULTS" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in
      ADMIN_PASSWORD|CODESERVER_PASSWORD|CARD_INITIAL_OWNERS|GITLAB_ROOT_PASSWORD|CSP_SECRET_KEY|CSP_SERVICE_TOKEN|CSP_DB_PASSWORD|CSP_APP_DB_PASSWORD|INTERNAL_PLATFORM_API_KEY|N8N_OWNER_EMAIL|N8N_OWNER_FIRST_NAME|N8N_OWNER_LAST_NAME|N8N_OWNER_PASSWORD_HASH|N8N_ENCRYPTION_KEY)
        _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"   # 去頭尾引號
        printf -v "$_k" '%s' "$_v" ;;                                # 賦值,非 eval
      *) : ;;
    esac
  done < "$DEFAULTS"
  ok "已套用 $DEFAULTS 的預設值(僅限白名單 KEY)"
fi

if [ "$REGEN" = 1 ]; then
  info "  secret:有預設用預設,否則 openssl 隨機生成"
  set_env_single_quoted CSP_SECRET_KEY            "${CSP_SECRET_KEY:-$(openssl rand -hex 32)}"
  set_env_single_quoted CSP_SERVICE_TOKEN         "${CSP_SERVICE_TOKEN:-$(openssl rand -hex 32)}"
  set_env_single_quoted INTERNAL_PLATFORM_API_KEY "${INTERNAL_PLATFORM_API_KEY:-sk-internal-$(openssl rand -hex 24)}"
  set_env_single_quoted ADMIN_PASSWORD            "${ADMIN_PASSWORD:-$(openssl rand -base64 24)}"
  set_env_single_quoted CSP_DB_PASSWORD           "${CSP_DB_PASSWORD:-$(openssl rand -hex 32)}"
  set_env_single_quoted CSP_APP_DB_PASSWORD       "${CSP_APP_DB_PASSWORD:-$(openssl rand -hex 32)}"
  set_env_single_quoted CODESERVER_PASSWORD       "${CODESERVER_PASSWORD:-$(openssl rand -base64 24)}"
fi

# 舊版 .env 可能把 secret 以裸 KEY=VALUE 寫入；Compose dotenv 會對 `$` 做
# interpolation，空白與 `#` 也可能改變實際值。每次部署都把既有值正規化成
# literal 單引號，確保密碼管理器中的內容與 container 收到的內容一致。
for _secret_name in CSP_SECRET_KEY CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY \
  ADMIN_PASSWORD CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD; do
  _secret_value="$(get_env_unquoted "$_secret_name")"
  [ -z "$_secret_value" ] || set_env_single_quoted "$_secret_name" "$_secret_value"
done

# 正式 private key material 必須位於 repo 外。每次重跑都重申該路徑，避免
# 舊 .env 把 Compose 拉回 ./secrets 或 infra/nginx/certs。
set_env ANILA_STATE_DIR "$ANILA_STATE_DIR"
set_env ANILA_SECRETS_DIR "$SECRETS_DIR"
set_env ANILA_TLS_CERTS_DIR "$TLS_CERTS_DIR"

# n8n 2.x fresh-install owner 由 env 管理。bcrypt 的 '$' 必須以 .env 單引號
# 保留；腳本不接收明碼，也不在 console 回顯 hash。
_n8n_owner_email="${N8N_OWNER_EMAIL:-$(get_env_unquoted N8N_OWNER_EMAIL)}"
case "$_n8n_owner_email" in ""|\<*\>) _n8n_owner_email="$(ask 'N8N_OWNER_EMAIL — n8n 管理者信箱')" ;; esac
[[ "$_n8n_owner_email" =~ ^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*ncsist\.org\.tw$ ]] \
  || die "N8N_OWNER_EMAIL 必須是正式 ncsist.org.tw 信箱"
set_env N8N_OWNER_EMAIL "$_n8n_owner_email"
set_env N8N_OWNER_FIRST_NAME "${N8N_OWNER_FIRST_NAME:-$(get_env_unquoted N8N_OWNER_FIRST_NAME)}"
set_env N8N_OWNER_LAST_NAME "${N8N_OWNER_LAST_NAME:-$(get_env_unquoted N8N_OWNER_LAST_NAME)}"
[ -n "$(get_env N8N_OWNER_FIRST_NAME)" ] || set_env N8N_OWNER_FIRST_NAME ANILA
[ -n "$(get_env N8N_OWNER_LAST_NAME)" ] || set_env N8N_OWNER_LAST_NAME Owner

_n8n_owner_hash="${N8N_OWNER_PASSWORD_HASH:-$(get_env_unquoted N8N_OWNER_PASSWORD_HASH)}"
case "$_n8n_owner_hash" in ""|\<*\>)
  _n8n_owner_hash="$(asksecret 'N8N_OWNER_PASSWORD_HASH — 完整 bcrypt hash (不是明碼)')" ;;
esac
if [[ "$_n8n_owner_hash" == \'*\' ]] || [[ "$_n8n_owner_hash" == \"*\" ]]; then
  _n8n_owner_hash="${_n8n_owner_hash:1:${#_n8n_owner_hash}-2}"
fi
[[ "$_n8n_owner_hash" =~ ^\$2[aby]\$([0-9]{2})\$[./A-Za-z0-9]{53}$ ]] \
  || die "N8N_OWNER_PASSWORD_HASH 必須是完整 bcrypt hash"
(( 10#${BASH_REMATCH[1]} >= 12 )) || die "N8N_OWNER_PASSWORD_HASH bcrypt cost 必須 >= 12"
set_env_single_quoted N8N_OWNER_PASSWORD_HASH "$_n8n_owner_hash"

_n8n_key="${N8N_ENCRYPTION_KEY:-$(get_env_unquoted N8N_ENCRYPTION_KEY)}"
case "$_n8n_key" in \<*\>) _n8n_key="" ;; esac
if [ -z "$_n8n_key" ]; then
  if docker volume inspect anila-platform_n8n_data >/dev/null 2>&1; then
    die "既有 n8n_data 但 .env 沒有 N8N_ENCRYPTION_KEY；請先從既有 /home/node/.n8n/config 安全取回原 key，不可隨機重生"
  fi
  _n8n_key="$(openssl rand -hex 32)"
fi
set_env_single_quoted N8N_ENCRYPTION_KEY "$_n8n_key"

_gitlab_root="${GITLAB_ROOT_PASSWORD:-$(get_env_unquoted GITLAB_ROOT_PASSWORD)}"
case "$_gitlab_root" in ""|\<*\>) _gitlab_root="$(openssl rand -base64 32)" ;; esac
set_env_single_quoted GITLAB_ROOT_PASSWORD "$_gitlab_root"

# 內網 strict 模式 + 卡片登入 + 模型 CA 路徑(每次都確保正確)
set_env ANILA_ALLOW_DEV_SECRET      0
set_env ANILA_DEPLOYMENT_PROFILE    prod-intranet-card
set_env ANILA_ALLOW_HTTP_ENDPOINT   0
set_env ANILA_ALLOW_PRIVATE_ENDPOINT 0
# Slice 6 旗標分域:ANILA_ENV=production → 「模型」http 一律 fail-closed(不受
# 任何旗標放行);MLSteam agent 是純 http NodePort → agent 專用旗標開 1。
set_env ANILA_ENV                   production
set_env ANILA_ALLOW_HTTP_AGENT_ENDPOINT 1
set_env ENABLE_CARD_LOGIN           true
set_env REQUIRE_CARD_LOGIN_ONLY     true
set_env ENABLE_PUBLIC_SHARE         false
set_env ENABLE_MEMORY               false
set_env PUBLIC_SHARE_MAX_TTL_HOURS  168
set_env ANILA_TRACE_ENDPOINT        http://csp:8000
_formal_host="$(get_env ANILA_HOST)"
set_env SITE_URL                    "https://${_formal_host:-anila.ai.ncsist.org.tw}"
set_env N8N_HOST                    n8n.ai.ncsist.org.tw
set_env N8N_EDITOR_BASE_URL         https://n8n.ai.ncsist.org.tw/
set_env N8N_WEBHOOK_URL             https://n8n.ai.ncsist.org.tw/
set_env N8N_TLS_REJECT_UNAUTHORIZED 1
set_env GITLAB_HOST                 gitlab.ai.ncsist.org.tw
set_env CODESERVER_HOST             code.ai.ncsist.org.tw
# GitLab HTTPS uses its native authentication. SSH remains the machine/developer
# Git path and must bind only the platform LAN interface,never 0.0.0.0.
[ -n "$(get_env GITLAB_SSH_BIND_IP)" ] || set_env GITLAB_SSH_BIND_IP 10.53.100.15
[ -n "$(get_env GITLAB_SSH_PORT)" ] || set_env GITLAB_SSH_PORT 2222
case "$(get_env GITLAB_SSH_BIND_IP)" in
  0.0.0.0|::) die "GITLAB_SSH_BIND_IP 不得綁所有介面,請填平台 LAN IP" ;;
esac
_gitlab_ssh_bind="$(get_env GITLAB_SSH_BIND_IP)"
_gitlab_ssh_port="$(get_env GITLAB_SSH_PORT)"
set_env GITLAB_SSH_BIND_IP          "${_gitlab_ssh_bind:-10.53.100.15}"
set_env GITLAB_SSH_PORT             "${_gitlab_ssh_port:-2222}"
# Dev/test bypasses must not survive an old .env into the formal card profile.
unset_env CARD_DEV_SKIP_NONCE_BINDING
unset_env SKIP_STARTUP_MIGRATIONS
unset_env ANILA_BREAK_GLASS_OWNER
unset_env ANILA_BREAK_GLASS_TICKET
unset_env ANILA_BREAK_GLASS_EXPIRES_AT
# 只在 model-ca.pem 真的有憑證時才指過去。ANILA_MODEL_CA_FILE → csp 的 SSL_CERT_FILE,
# 而 SSL_CERT_FILE 是「取代」整個系統信任庫(非疊加):指到空/壞檔 → csp 所有出向 https
# 全 CERTIFICATE_VERIFY_FAILED(連 agent 都連不上)。空字串則 fallback 系統 CA,無副作用。
if [ -s "$MCA" ] && grep -q 'BEGIN CERTIFICATE' "$MCA" 2>/dev/null; then
  set_env ANILA_MODEL_CA_FILE       /etc/anila/pki/model-ca.pem
else
  set_env ANILA_MODEL_CA_FILE       ""
  warn "model-ca.pem 無有效憑證 → 暫不設 ANILA_MODEL_CA_FILE(csp 用系統 CA);補好 CA 再 up -d csp"
fi
# code-server workspace 現已在 compose 固定為 share/codeserver-sandbox。
# 移除舊版 repo-root 覆寫,避免 operator 誤以為它仍生效。
if grep -q '^CODESERVER_WORKSPACE=' .env 2>/dev/null; then
  unset_env CODESERVER_WORKSPACE
  warn "已移除過時的 CODESERVER_WORKSPACE(repo-root 掛載已停用)"
fi

echo
# owner 是最高權限種子帳號。不讓「被竄改的 bundle 預設 + 一個 Enter」就生效:
# 有預設就明確顯示出來、要求打 y 確認;否則一律手動輸入。
DEF_CIO="${CARD_INITIAL_OWNERS:-$(get_env_unquoted CARD_INITIAL_OWNERS)}"
if [ -n "$DEF_CIO" ] && \
   [ "$(ask "偵測到預設 owner 員工編號 '$(c '1;33' "$DEF_CIO")' — 確定用這組?(N 則手動輸入) [y/N]" N)" = y ]; then
  CIO="$DEF_CIO"
else
  CIO="$(ask 'CARD_INITIAL_OWNERS — owner 員工編號 (CSV,含你自己,例 990000002,990000001)')"
fi
[ -n "$CIO" ] || die "CARD_INITIAL_OWNERS 不能空(否則沒人是 owner,進不了管理)"
ok "owner = $CIO"
set_env_single_quoted CARD_INITIAL_OWNERS "$CIO"

MGK="$(asksecret 'MODEL_GATEWAY_API_KEY — 在 .12 gateway 簽發的 key (還沒有就 Enter 跳過)')"
if [ -n "$MGK" ]; then set_env_single_quoted MODEL_GATEWAY_API_KEY "$MGK"; ok "已設 MODEL_GATEWAY_API_KEY"
else warn "MODEL_GATEWAY_API_KEY 留空 — 模型 proxy 暫時打不通。拿到後用 anila-ops.sh gateway-key 安全套用"; fi

# 必填齊全檢查
for k in CSP_SECRET_KEY CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY ADMIN_PASSWORD \
  CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD CARD_INITIAL_OWNERS \
         SITE_URL N8N_HOST N8N_EDITOR_BASE_URL N8N_WEBHOOK_URL GITLAB_HOST \
         N8N_OWNER_EMAIL N8N_OWNER_PASSWORD_HASH N8N_ENCRYPTION_KEY \
         GITLAB_ROOT_PASSWORD CODESERVER_HOST GITLAB_SSH_BIND_IP GITLAB_SSH_PORT \
         ANILA_SECRETS_DIR ANILA_TLS_CERTS_DIR; do
  [ -n "$(get_env "$k")" ] || die ".env 缺必填值: $k"
done
for _secret_name in CSP_SECRET_KEY CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY \
  ADMIN_PASSWORD CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD \
  N8N_ENCRYPTION_KEY GITLAB_ROOT_PASSWORD; do
  _secret_value="$(get_env_unquoted "$_secret_name")"
  case "$_secret_value" in
    ""|\<*\>|*changeme*|*placeholder*|*example*) die "$_secret_name 仍是空值/dev/template 值" ;;
  esac
done
[ "$(get_env N8N_TLS_REJECT_UNAUTHORIZED)" = 1 ] || die "n8n TLS 驗證必須開啟"
(( ${#_n8n_key} >= 32 )) || die "N8N_ENCRYPTION_KEY 長度必須 >= 32"
(( ${#_gitlab_root} >= 16 )) || die "GITLAB_ROOT_PASSWORD 長度必須 >= 16"
_codeserver_password="$(get_env_unquoted CODESERVER_PASSWORD)"
(( ${#_codeserver_password} >= 16 )) || die "CODESERVER_PASSWORD 長度必須 >= 16"
ok ".env 就緒 (strict + 卡片登入 + 模型走 .12 gateway)"

if [ "$REGEN" = 1 ]; then
  echo
  echo "$(c '1;33' '──── 請把以下 secret 存進密碼管理器(只顯示這一次) ────')"
  for k in ADMIN_PASSWORD CSP_SECRET_KEY CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY \
           CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD \
           N8N_ENCRYPTION_KEY GITLAB_ROOT_PASSWORD; do
    printf '  %-26s %s\n' "$k" "$(get_env_unquoted "$k")"
  done
  echo "$(c '1;33' '──────────────────────────────────────────────────────')"
  read -rp "存好了按 Enter 繼續 ... " _
fi

# ── 4. load image ────────────────────────────────────────────────────────
info "[4/7] load image (SHA256 驗檔 + re-tag anila-intranet-* → anila-platform-*)"
bash "$BUNDLE/INTRANET-LOAD.sh"

# Gate 1 F6: resolve every formal Compose image to the exact content ID in the
# checksummed bundle lock.  These values persist in .env, so later recreates
# cannot drift when a mutable tag is retargeted on the host.
IMAGE_LOCK_SCRIPT="infra/deployment/scripts/verify-compose-image-lock.py"
while IFS=$'\t' read -r image_variable image_id; do
  [ -n "$image_variable" ] && set_env "$image_variable" "$image_id"
done < <(python3 "$IMAGE_LOCK_SCRIPT" emit-env \
  --lock "$BUNDLE/PLATFORM-IMAGE-LOCK.tsv")
python3 "$IMAGE_LOCK_SCRIPT" verify-env --env-file .env \
  --lock "$BUNDLE/PLATFORM-IMAGE-LOCK.tsv" --inspect-docker \
  || die "formal Compose image content-ID lock 驗證失敗"
ok "formal Compose default services 已鎖定 bundle sha256 image IDs"

# ── 4b. JWT 簽章金鑰 ───────────────────────────────────────────────────────
# csp 用這把 RSA 私鑰簽登入 access token,並對 anila-studio 等服務發 JWKS 公鑰。
# prod 模式 ALLOW_AUTO_KEYGEN=false → 不自動生;缺這把:csp /.well-known/jwks.json
# 回 500、登入發不了 token、anila-studio 啟動 crash-loop。compose 以 :ro 把
# repo 外 ANILA_SECRETS_DIR mount 進 csp:/app/secrets。必須在 [6] up 之前產好。
info "[4b/7] JWT 簽章金鑰 ($SECRETS_DIR/jwt-private.pem)"
mkdir -p "$SECRETS_DIR"
prepare_csp_runtime_mount "$SECRETS_DIR" 700
prepare_csp_runtime_mount "$PWD/share/uploads/ingestion" 700
# Host operator 無法 traverse UID 10001 + mode 0700 的 secrets。由 CSP runtime
# user 在無網路、唯讀 rootfs 的 one-shot container 內驗證/沿用/首次生成；
# partial、symlink、malformed、mismatched pair 一律 fail-closed。
docker run --rm --pull never --network none --read-only \
  -v "$SECRETS_DIR:/out" --entrypoint python "$(get_env ANILA_IMAGE_CSP)" \
  /app/scripts/generate-jwt-keypair.py --output-dir /out --ensure \
  && ok "JWT keypair 已驗證/就緒 (RSA-2048 / RS256 / PKCS#8)" \
  || die "JWT keypair ensure 失敗 (csp image / keypair 完整性 / 權限?)"
# Re-assert owner/modes for pre-existing keys created by older root images and
# for newly generated files.  Public key is readable; private key is CSP-only.
docker run --rm --pull never --user 0:0 --network none --read-only \
  -v "$SECRETS_DIR:/mnt" \
  --entrypoint sh "$(get_env ANILA_IMAGE_CSP)" \
  -c "chown $CSP_RUNTIME_UID:$CSP_RUNTIME_GID /mnt /mnt/jwt-private.pem /mnt/jwt-public.pem && chmod 700 /mnt && chmod 600 /mnt/jwt-private.pem && chmod 644 /mnt/jwt-public.pem" \
  || die "JWT keypair 權限收斂失敗"

# ── 5. network ───────────────────────────────────────────────────────────
info "[5/7] docker network anila-models-net"
docker network inspect anila-models-net >/dev/null 2>&1 \
  && ok "已存在" \
  || { docker network create anila-models-net >/dev/null && ok "已建立"; }

# ── 6. up ────────────────────────────────────────────────────────────────
info "[6/7] docker compose up -d --no-build --pull never"
bash infra/deployment/scripts/deploy-prod.sh tool-preflight
docker compose up -d --no-build --pull never

# ── 7. 驗證 ──────────────────────────────────────────────────────────────
info "[7/7] 等全 stack ready + fail-closed 驗證"
bash infra/deployment/scripts/deploy-prod.sh wait
bash infra/deployment/scripts/deploy-prod.sh postconfigure
bash infra/deployment/scripts/deploy-prod.sh verify

echo
echo "============================================================"
ok "內網部署完成"
echo "  • 登入:員工從瀏覽器插卡 + HiPKI(localhost:16888)走卡片登入"
echo "  • 日常:infra/deployment/scripts/deploy-prod.sh {status | logs <svc> | restart | down}"
echo "  • 維運:infra/deployment/scripts/anila-ops.sh {health | backup | restore | cert-renew | break-glass}"
echo "  • n8n:    https://n8n.ai.ncsist.org.tw/ (原生帳號；webhook ingress 封鎖)"
echo "  • GitLab: https://gitlab.ai.ncsist.org.tw/ (原生帳號/PAT；SSH :$(get_env GITLAB_SSH_PORT))"
echo "  • 開發 IDE:https://code.ai.ncsist.org.tw/ (先放獨立 clone,再執行 deploy-prod.sh codeserver-up)"
[ -z "${MGK:-}" ] && echo "  • $(c '1;33' '待辦'):MODEL_GATEWAY_API_KEY 拿到後執行 anila-ops.sh gateway-key"
echo "  • DNS:確認 anila/n8n/gitlab/code.ai.ncsist.org.tw → 本機、aiagent2.ai.ncsist.org.tw → .12"
echo "============================================================"
