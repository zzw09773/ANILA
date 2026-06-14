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
#   [6] docker compose up -d --no-build
#   [7] 等 healthy + 驗證
#
# 用法 (在 prod-intranet-card repo 根目錄):
#   bash scripts/intranet-deploy.sh [IMAGE_BUNDLE_DIR]
#   IMAGE_BUNDLE_DIR 預設自動找 ./intranet-prod-v1.0.0;找不到會提示輸入。
#
# 重跑安全:偵測到既有 .env 預設「保留現有 secret」— 避免重生 DB 密碼炸掉既有
# DB。只有你明確選擇重生才會換 secret(並先備份舊 .env)。
#
# 注意:這支不碰 model image / 權重(第一版走 .12 gateway)。後續日常操作用
# scripts/deploy-prod.sh {status|logs|restart|down}。
# ============================================================================
set -euo pipefail

c()    { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info() { echo "$(c '1;36' '▶') $*"; }
ok()   { echo "$(c '1;32' '✓') $*"; }
warn() { echo "$(c '1;33' '⚠') $*"; }
die()  { echo "$(c '1;31' '✗') $*" >&2; exit 1; }
ask()       { local p="$1" d="${2:-}" a; read -rp "$(c '1;35' '?') ${p}${d:+ [$d]}: " a; printf '%s' "${a:-$d}"; }
asksecret() { local p="$1" a; read -rsp "$(c '1;35' '?') ${p}: " a; echo >&2; printf '%s' "$a"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

set_env() {  # set_env KEY VALUE — 去重後 append (literal,不怕特殊字元/sed 跳脫)
  local key="$1" val="$2"
  [ -f .env ] || die ".env 不存在"
  grep -vE "^${key}=" .env > .env.tmp 2>/dev/null || true
  mv .env.tmp .env
  printf '%s=%s\n' "$key" "$val" >> .env
}
get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

echo "============================================================"
echo " ANILA 內網一條龍部署 — V1.0.0 (prod-intranet-card / 卡片登入)"
echo "============================================================"

# ── 0. 前置檢查 ───────────────────────────────────────────────────────────
info "[0/7] 前置檢查"
command -v docker >/dev/null   || die "找不到 docker"
command -v openssl >/dev/null  || die "找不到 openssl"
docker info >/dev/null 2>&1    || die "docker daemon 沒在跑 / 當前使用者無權限"
[ -f docker-compose.yml ] && [ -f .env.example ] \
  || die "請在 prod-intranet-card repo 根目錄執行(找不到 docker-compose.yml / .env.example)"
br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
[ "$br" = prod-intranet-card ] || warn "目前 git 分支是 '$br',預期 prod-intranet-card — 確認 checkout 對了"

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
mkdir -p myCSPPlatform/docker/certs
CRT=myCSPPlatform/docker/certs/server.crt
KEY=myCSPPlatform/docker/certs/server.key
DO_TLS=1
if [ -f "$CRT" ] && [ -f "$KEY" ]; then
  warn "已有 server.crt / server.key"
  [ "$(ask '重新從 pfx 抽取?(會覆蓋) [y/N]' N)" = y ] || DO_TLS=0
fi
if [ "$DO_TLS" = 1 ]; then
  PFX="$(ask 'server.pfx 路徑')"; [ -f "$PFX" ] || die "找不到 $PFX"
  PFXPW="$(asksecret 'pfx 密碼 (空就直接 Enter)')"
  openssl pkcs12 -in "$PFX" -clcerts -nokeys  -legacy -passin "pass:$PFXPW" | openssl x509 > "$CRT" || die "抽 server.crt 失敗"
  openssl pkcs12 -in "$PFX" -nocerts  -noenc   -legacy -passin "pass:$PFXPW" | openssl pkey  > "$KEY" || die "抽 server.key 失敗"
  chmod 600 "$KEY"
  ok "TLS 憑證抽取完成"
fi
echo "    $(openssl x509 -in "$CRT" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
openssl x509 -in "$CRT" -noout -subject 2>/dev/null | grep -q 'ai.ncsist.org.tw' \
  || warn "subject 看起來不是 *.ai.ncsist.org.tw — 確認憑證對不對"

# ── 2. 模型 gateway 出向 CA ───────────────────────────────────────────────
info "[2/7] 模型 gateway CA (對 https://aiagent2.ai.ncsist.org.tw 的出向 TLS 信任)"
mkdir -p share/pki
MCA=share/pki/model-ca.pem
if [ -f "$MCA" ]; then
  ok "已有 $MCA"
else
  echo "    試從內網 NCSIST repo 下載 NCSISTCA.cer ..."
  if curl -fsS -o /tmp/NCSISTCA.cer http://repository.ncsist.org.tw/certs/NCSISTCA.cer 2>/dev/null; then
    openssl x509 -inform der -in /tmp/NCSISTCA.cer -out "$MCA" 2>/dev/null || cp /tmp/NCSISTCA.cer "$MCA"
    ok "取得 model-ca.pem"
  else
    warn "NCSIST repo 連不到"
    P="$(ask 'model CA PEM 路徑 (IT 給的;留空則稍後手動放 share/pki/model-ca.pem)')"
    if [ -n "$P" ] && [ -f "$P" ]; then cp "$P" "$MCA"; ok "已複製 model-ca.pem"
    else warn "略過 — 部署前務必把 model CA 放到 $MCA,否則 csp 連 gateway 會 TLS 失敗"; fi
  fi
fi

# ── 3. .env ──────────────────────────────────────────────────────────────
info "[3/7] 產生 / 更新 .env"
REGEN=1
if [ -f .env ]; then
  warn "已有 .env"
  if [ "$(ask '保留現有 secret?(建議 Y — 重生 DB 密碼會炸掉既有 DB) [Y/n]' Y)" = n ]; then
    cp .env ".env.bak.$(date +%s)"; warn "已備份舊 .env;將重生 secret"
  else
    REGEN=0
  fi
else
  cp .env.example .env; ok "由 .env.example 建立 .env"
fi

if [ "$REGEN" = 1 ]; then
  info "  自動生成 secret (openssl rand)"
  set_env CSP_SECRET_KEY            "$(openssl rand -hex 32)"
  set_env CSP_SERVICE_TOKEN         "$(openssl rand -hex 32)"
  set_env INTERNAL_PLATFORM_API_KEY "sk-internal-$(openssl rand -hex 24)"
  set_env ADMIN_PASSWORD            "$(openssl rand -base64 24)"
  set_env CSP_DB_PASSWORD           "$(openssl rand -hex 32)"
  set_env CSP_APP_DB_PASSWORD       "$(openssl rand -hex 32)"
  set_env CODESERVER_PASSWORD       "$(openssl rand -base64 24)"
fi

# 內網 strict 模式 + 卡片登入 + 模型 CA 路徑(每次都確保正確)
set_env ANILA_ALLOW_DEV_SECRET      0
set_env ANILA_ALLOW_HTTP_ENDPOINT   0
set_env ANILA_ALLOW_PRIVATE_ENDPOINT 0
set_env ENABLE_CARD_LOGIN           true
set_env REQUIRE_CARD_LOGIN_ONLY     true
set_env ANILA_MODEL_CA_FILE         /etc/anila/pki/model-ca.pem

echo
CIO="$(ask 'CARD_INITIAL_OWNERS — owner 員工編號 (CSV,含你自己,例 1147259,1090868)' "$(get_env CARD_INITIAL_OWNERS)")"
[ -n "$CIO" ] || die "CARD_INITIAL_OWNERS 不能空(否則沒人是 owner,進不了管理)"
set_env CARD_INITIAL_OWNERS "$CIO"

MGK="$(asksecret 'MODEL_GATEWAY_API_KEY — 在 .12 gateway 簽發的 key (還沒有就 Enter 跳過)')"
if [ -n "$MGK" ]; then set_env MODEL_GATEWAY_API_KEY "$MGK"; ok "已設 MODEL_GATEWAY_API_KEY"
else warn "MODEL_GATEWAY_API_KEY 留空 — 模型 proxy 暫時打不通。拿到後填進 .env 再 'docker compose up -d csp'"; fi

# 必填齊全檢查
for k in CSP_SECRET_KEY CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY ADMIN_PASSWORD \
         CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD CODESERVER_WORKSPACE CARD_INITIAL_OWNERS; do
  [ -n "$(get_env "$k")" ] || die ".env 缺必填值: $k"
done
ok ".env 就緒 (strict + 卡片登入 + 模型走 .12 gateway)"

if [ "$REGEN" = 1 ]; then
  echo
  echo "$(c '1;33' '──── 請把以下 secret 存進密碼管理器(只顯示這一次) ────')"
  for k in ADMIN_PASSWORD CSP_SECRET_KEY CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY \
           CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD; do
    printf '  %-26s %s\n' "$k" "$(get_env "$k")"
  done
  echo "$(c '1;33' '──────────────────────────────────────────────────────')"
  read -rp "存好了按 Enter 繼續 ... " _
fi

# ── 4. load image ────────────────────────────────────────────────────────
info "[4/7] load image (SHA256 驗檔 + re-tag anila-intranet-* → anila-platform-*)"
bash "$BUNDLE/INTRANET-LOAD.sh"

# ── 5. network ───────────────────────────────────────────────────────────
info "[5/7] docker network anila-models-net"
docker network inspect anila-models-net >/dev/null 2>&1 \
  && ok "已存在" \
  || { docker network create anila-models-net >/dev/null && ok "已建立"; }

# ── 6. up ────────────────────────────────────────────────────────────────
info "[6/7] docker compose up -d --no-build"
docker compose up -d --no-build

# ── 7. 驗證 ──────────────────────────────────────────────────────────────
info "[7/7] 等 csp healthy + 驗證"
CID="$(docker compose ps -q csp 2>/dev/null || true)"
h='?'
for _ in $(seq 1 40); do
  h="$(docker inspect "$CID" --format '{{.State.Health.Status}}' 2>/dev/null || echo '?')"
  [ "$h" = healthy ] && break
  sleep 3
done
echo "    csp health: $h"
[ "$h" = healthy ] || warn "csp 未在預期時間內 healthy — 查 log: docker compose logs csp | tail -80"
curl -skf -o /dev/null -w "    nginx https(443): %{http_code}\n" https://localhost/ 2>/dev/null \
  || warn "nginx 443 未回應(可能還在起,稍等再試 curl -sk https://localhost/health)"

echo
echo "============================================================"
ok "內網部署完成"
echo "  • 登入:員工從瀏覽器插卡 + HiPKI(localhost:16888)走卡片登入"
echo "  • 日常:scripts/deploy-prod.sh {status | logs <svc> | restart | down}"
[ -z "${MGK:-}" ] && echo "  • $(c '1;33' '待辦'):MODEL_GATEWAY_API_KEY 拿到後填 .env → docker compose up -d csp"
echo "  • DNS:確認 anila.ai.ncsist.org.tw → 本機、aiagent2.ai.ncsist.org.tw → .12"
echo "============================================================"
