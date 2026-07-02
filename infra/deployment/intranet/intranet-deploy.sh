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

c()    { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info() { echo "$(c '1;36' '▶') $*"; }
ok()   { echo "$(c '1;32' '✓') $*"; }
warn() { echo "$(c '1;33' '⚠') $*"; }
die()  { echo "$(c '1;31' '✗') $*" >&2; exit 1; }
ask()       { local p="$1" d="${2:-}" a; read -rp "$(c '1;35' '?') ${p}${d:+ [$d]}: " a; printf '%s' "${a:-$d}"; }
asksecret() { local p="$1" a; read -rsp "$(c '1;35' '?') ${p}: " a; echo >&2; printf '%s' "$a"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
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
[ -f compose.yaml ] && [ -f .env.example ] \
  || die "請在 prod-intranet-card repo 根目錄執行(找不到 compose.yaml / .env.example)"
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
mkdir -p infra/nginx/certs
CRT=infra/nginx/certs/server.crt
KEY=infra/nginx/certs/server.key
DO_TLS=1
if [ -f "$CRT" ] && [ -f "$KEY" ]; then
  warn "已有 server.crt / server.key"
  [ "$(ask '重新從 pfx 抽取?(會覆蓋) [y/N]' N)" = y ] || DO_TLS=0
fi
if [ "$DO_TLS" = 1 ]; then
  PFX="$(ask 'server.pfx 路徑')"; [ -f "$PFX" ] || die "找不到 $PFX"
  PFXPW="$(asksecret 'pfx 密碼 (空就直接 Enter)')"
  # server.crt 抽 fullchain:leaf(client cert)在前 + pfx 內所有中繼 CA,瀏覽器才能
  # 驗完整鏈(只放 leaf 在缺中繼的環境會跳「憑證不受信任」)。sed 只留 PEM 區塊,
  # 去掉 openssl 的 Bag Attributes 雜訊。
  {
    openssl pkcs12 -in "$PFX" -clcerts -nokeys -legacy -passin "pass:$PFXPW" 2>/dev/null
    openssl pkcs12 -in "$PFX" -cacerts -nokeys -legacy -passin "pass:$PFXPW" 2>/dev/null
  } | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' > "$CRT"
  [ -s "$CRT" ] && grep -q 'BEGIN CERTIFICATE' "$CRT" || die "抽 server.crt 失敗(pfx 路徑/密碼?)"
  openssl pkcs12 -in "$PFX" -nocerts -noenc -legacy -passin "pass:$PFXPW" 2>/dev/null | openssl pkey > "$KEY" || die "抽 server.key 失敗"
  chmod 600 "$KEY"
  _ncert=$(grep -c 'BEGIN CERTIFICATE' "$CRT")
  ok "TLS 憑證抽取完成(fullchain $_ncert 張:leaf + $((_ncert - 1)) 中繼)"
fi
echo "    $(openssl x509 -in "$CRT" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
openssl x509 -in "$CRT" -noout -subject 2>/dev/null | grep -q 'ai.ncsist.org.tw' \
  || warn "subject 看起來不是 *.ai.ncsist.org.tw — 確認憑證對不對"

# ── 2. 模型 gateway 出向 CA ───────────────────────────────────────────────
info "[2/7] 模型 gateway CA (對 https://aiagent2.ai.ncsist.org.tw 的出向 TLS 信任)"
# share/* 是 nginx(static)、csp + ingestion-worker(uploads)的 bind-mount 來源;
# 先建好,否則 docker 會以 root 自動建空目錄(權限/擁有者錯亂)。
mkdir -p share/pki share/static share/uploads/ingestion
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
    cp .env ".env.bak.$(date +%s)"; warn "已備份舊 .env;將重生 secret"
  else
    REGEN=0
  fi
else
  cp .env.example .env; ok "由 .env.example 建立 .env"
fi

# 預設值來源(選用):image 包裡的 intranet-defaults.env(刻意不進 git,實體隨包帶入
# air-gap)。提供 ADMIN_PASSWORD / CODESERVER_PASSWORD / CARD_INITIAL_OWNERS /
# GITLAB_ROOT_PASSWORD 等;沒提供的 secret 一律 openssl 隨機生成。
# 安全:不用 `source`(被竄改的 defaults 檔會執行任意指令),改嚴格解析 KEY=VALUE
# + 白名單;非白名單 / 註解 / 空行一律略過。
DEFAULTS="$BUNDLE/intranet-defaults.env"
if [ -f "$DEFAULTS" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in
      ADMIN_PASSWORD|CODESERVER_PASSWORD|CARD_INITIAL_OWNERS|GITLAB_ROOT_PASSWORD|CSP_SECRET_KEY|CSP_SERVICE_TOKEN|CSP_DB_PASSWORD|CSP_APP_DB_PASSWORD|INTERNAL_PLATFORM_API_KEY)
        _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"   # 去頭尾引號
        printf -v "$_k" '%s' "$_v" ;;                                # 賦值,非 eval
      *) : ;;
    esac
  done < "$DEFAULTS"
  ok "已套用 $DEFAULTS 的預設值(僅限白名單 KEY)"
fi

if [ "$REGEN" = 1 ]; then
  info "  secret:有預設用預設,否則 openssl 隨機生成"
  set_env CSP_SECRET_KEY            "${CSP_SECRET_KEY:-$(openssl rand -hex 32)}"
  set_env CSP_SERVICE_TOKEN         "${CSP_SERVICE_TOKEN:-$(openssl rand -hex 32)}"
  set_env INTERNAL_PLATFORM_API_KEY "${INTERNAL_PLATFORM_API_KEY:-sk-internal-$(openssl rand -hex 24)}"
  set_env ADMIN_PASSWORD            "${ADMIN_PASSWORD:-$(openssl rand -base64 24)}"
  set_env CSP_DB_PASSWORD           "${CSP_DB_PASSWORD:-$(openssl rand -hex 32)}"
  set_env CSP_APP_DB_PASSWORD       "${CSP_APP_DB_PASSWORD:-$(openssl rand -hex 32)}"
  set_env CODESERVER_PASSWORD       "${CODESERVER_PASSWORD:-$(openssl rand -base64 24)}"
  # gitlab root 初始密碼:與其他 secret 同 parity — 只在 REGEN(全新/重生)寫;
  # REGEN=0(保留現有)時不動,避免 re-run 偷改既有密碼。compose 用 env 帶入,
  # 只在 gitlab 首次 reconfigure 生效。
  [ -n "${GITLAB_ROOT_PASSWORD:-}" ] && set_env GITLAB_ROOT_PASSWORD "$GITLAB_ROOT_PASSWORD"
fi

# 內網 strict 模式 + 卡片登入 + 模型 CA 路徑(每次都確保正確)
set_env ANILA_ALLOW_DEV_SECRET      0
set_env ANILA_ALLOW_HTTP_ENDPOINT   0
set_env ANILA_ALLOW_PRIVATE_ENDPOINT 0
# Slice 6 旗標分域:ANILA_ENV=production → 「模型」http 一律 fail-closed(不受
# 任何旗標放行);MLSteam agent 是純 http NodePort → agent 專用旗標開 1。
set_env ANILA_ENV                   production
set_env ANILA_ALLOW_HTTP_AGENT_ENDPOINT 1
set_env ENABLE_CARD_LOGIN           true
set_env REQUIRE_CARD_LOGIN_ONLY     true
# 只在 model-ca.pem 真的有憑證時才指過去。ANILA_MODEL_CA_FILE → csp 的 SSL_CERT_FILE,
# 而 SSL_CERT_FILE 是「取代」整個系統信任庫(非疊加):指到空/壞檔 → csp 所有出向 https
# 全 CERTIFICATE_VERIFY_FAILED(連 agent 都連不上)。空字串則 fallback 系統 CA,無副作用。
if [ -s "$MCA" ] && grep -q 'BEGIN CERTIFICATE' "$MCA" 2>/dev/null; then
  set_env ANILA_MODEL_CA_FILE       /etc/anila/pki/model-ca.pem
else
  set_env ANILA_MODEL_CA_FILE       ""
  warn "model-ca.pem 無有效憑證 → 暫不設 ANILA_MODEL_CA_FILE(csp 用系統 CA);補好 CA 再 up -d csp"
fi
# codeserver workspace:下方必填檢查需要它;compose 搬到 infra/compose 後,
# 相對路徑以該目錄為基準,repo root = `../..`(與 .env.example 預設同值)。
[ -n "$(get_env CODESERVER_WORKSPACE)" ] || set_env CODESERVER_WORKSPACE ../..

echo
# owner 是最高權限種子帳號。不讓「被竄改的 bundle 預設 + 一個 Enter」就生效:
# 有預設就明確顯示出來、要求打 y 確認;否則一律手動輸入。
DEF_CIO="${CARD_INITIAL_OWNERS:-$(get_env CARD_INITIAL_OWNERS)}"
if [ -n "$DEF_CIO" ] && \
   [ "$(ask "偵測到預設 owner 員工編號 '$(c '1;33' "$DEF_CIO")' — 確定用這組?(N 則手動輸入) [y/N]" N)" = y ]; then
  CIO="$DEF_CIO"
else
  CIO="$(ask 'CARD_INITIAL_OWNERS — owner 員工編號 (CSV,含你自己,例 1147259,1090868)')"
fi
[ -n "$CIO" ] || die "CARD_INITIAL_OWNERS 不能空(否則沒人是 owner,進不了管理)"
ok "owner = $CIO"
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

# ── 4b. JWT 簽章金鑰 ───────────────────────────────────────────────────────
# csp 用這把 RSA 私鑰簽登入 access token,並對 anila-studio 等服務發 JWKS 公鑰。
# prod 模式 ALLOW_AUTO_KEYGEN=false → 不自動生;缺這把:csp /.well-known/jwks.json
# 回 500、登入發不了 token、anila-studio 啟動 crash-loop。compose 以 :ro 把 ./secrets
# mount 進 csp:/app/secrets。必須在 [6] up 之前產好。需 csp image(故排在 load 之後)。
info "[4b/7] JWT 簽章金鑰 (secrets/jwt-private.pem)"
mkdir -p secrets
if [ -f secrets/jwt-private.pem ] && [ -f secrets/jwt-public.pem ]; then
  ok "已有 JWT keypair (重跑沿用,token 不失效)"
else
  docker run --rm -v "$PWD/secrets:/out" --entrypoint python anila-platform-csp:latest \
    /app/scripts/generate-jwt-keypair.py --output-dir /out \
    && ok "已產生 JWT keypair (RSA-2048 / RS256 / PKCS#8)" \
    || die "JWT keypair 產生失敗 (csp image 在? scripts/generate-jwt-keypair.py 在?)"
fi

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
echo "  • 日常:infra/deployment/scripts/deploy-prod.sh {status | logs <svc> | restart | down}"
[ -z "${MGK:-}" ] && echo "  • $(c '1;33' '待辦'):MODEL_GATEWAY_API_KEY 拿到後填 .env → docker compose up -d csp"
echo "  • DNS:確認 anila.ai.ncsist.org.tw → 本機、aiagent2.ai.ncsist.org.tw → .12"
echo "============================================================"
