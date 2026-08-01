#!/usr/bin/env bash
# ============================================================================
# deploy-prod.sh
# ----------------------------------------------------------------------------
# ANILA Prod 部署腳本(支援 2026-05-26 重構後的 3 條 prod branch)
#
# 接受的 prod branch:
#   - prod-intranet-card   中科院內網 + PKI 自然人憑證卡 (SSO + card auth fork)
#   - prod-public-passwd   對外網 + 純帳密 (main + 外網 hardening)
#   - prod-military-passwd 國軍交付 + 純帳密 (main + military spec)
#
# 用法:
#   git checkout <prod-branch> && git pull
#   set -a; source /path/to/<branch>.env; set +a
#   bash infra/deployment/scripts/deploy-prod.sh [SUBCOMMAND]
#
# SUBCOMMAND:
#   preflight        只跑 pre-flight 檢查,不動 stack
#   deploy           preflight + build + up + wait healthy + verify  (預設)
#   up               docker compose up -d (不 rebuild)
#   down             docker compose down (保留 volumes,db 資料不丟)
#   restart          down + up
#   rebuild <svc>    rebuild + restart 單一 service (e.g. rebuild csp)
#   status           顯示所有 service health
#   logs <svc>       tail -f 單一 service logs
#   help             顯示這份說明
#
# 環境變數(必要,缺值 fail-loud):
#   CSP_SERVICE_TOKEN         service-to-service token,csp/router/anila-studio/
#                             ingestion-worker 都用同一把
#   INTERNAL_PLATFORM_API_KEY 內部 system worker API key,ingestion-worker /
#                             flux2-dev-agent 用
#   SECRET_KEY                JWT signing key + agent credential AES key
#
# 環境變數(可選,有合理 default):
#   LOCAL_LLM_MODEL / LOCAL_LLM_BASE_URL
#   LOCAL_EMBEDDING_MODEL / LOCAL_EMBEDDING_BASE_URL
#   ANILA_TRUSTED_HOSTS
#   ANILA_REMOTE_MODELS=1   模型在別台主機 (如內網 10.53.100.12):跳過本機
#                           model container 檢查,改 curl *_BASE_URL 探測
#
# 前置條件(腳本會自動 check):
#   1. 現在 git branch 是 `prod`(避免不小心在 main 上跑)
#   2. Docker daemon running
#   3. docker compose v2 可用
#   4. anila-models-net network 已存在(模型 stack 先起來)
#   5. 模型服務(gemma4 / flux2-dev / flux2-dev-agent / nv-embed-proxy)healthy
#   6. share/uploads/flux 目錄存在(flux2-dev-agent 寫圖檔用)
#   7. 必要 env 已設且非 dev fallback
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

# ── 顏色 helper ────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'
  C_B=$'\033[34m'; C_M=$'\033[35m'; C_C=$'\033[36m'; C_N=$'\033[0m'
else
  C_R=""; C_G=""; C_Y=""; C_B=""; C_M=""; C_C=""; C_N=""
fi

log()   { printf "%s[%s]%s %s\n" "$C_C" "$(date +%H:%M:%S)" "$C_N" "$*"; }
ok()    { printf "  %s✅%s %s\n" "$C_G" "$C_N" "$*"; }
warn()  { printf "  %s⚠️%s  %s\n" "$C_Y" "$C_N" "$*"; }
err()   { printf "  %s❌%s %s\n" "$C_R" "$C_N" "$*"; }
fatal() { err "$*"; exit 1; }

section() {
  printf "\n%s════════════════════════════════════════════════════════════════════%s\n" "$C_B" "$C_N"
  printf "%s %s %s\n" "$C_B" "$*" "$C_N"
  printf "%s════════════════════════════════════════════════════════════════════%s\n" "$C_B" "$C_N"
}

# ── Pre-flight: 環境 ──────────────────────────────────────────────────────
# 接受的 prod branch 清單(2026-05-26 重構後從 1 條變 3 條)。
# 防呆:在 main / dev-* / feature/* 上跑這腳本會被擋掉。
_PROD_BRANCHES=(prod-intranet-card prod-public-passwd prod-military-passwd)

check_branch() {
  local branch
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  local matched=0
  for b in "${_PROD_BRANCHES[@]}"; do
    [[ "$branch" == "$b" ]] && matched=1 && break
  done
  if (( matched == 0 )); then
    err "目前在 '$branch' 分支,prod 部署必須切到下列其中一條:"
    for b in "${_PROD_BRANCHES[@]}"; do err "  - $b"; done
    fatal "請執行: git checkout <branch> && git pull origin <branch>"
  fi
  ok "git branch = $branch"
  # 對應分支特性簡述,讓 user 確認沒切錯
  case "$branch" in
    prod-intranet-card)   ok "  特性: 中科院內網 + PKI 自然人憑證卡(SSO + card auth fork)" ;;
    prod-public-passwd)   ok "  特性: 對外網 + 純帳密(main + 外網 hardening)" ;;
    prod-military-passwd) ok "  特性: 國軍交付 + 純帳密(main + military spec)" ;;
  esac
}

check_docker() {
  command -v docker >/dev/null || fatal "未找到 docker"
  docker info >/dev/null 2>&1 || fatal "docker daemon 沒在跑(或當前 user 無權限)"
  ok "docker daemon healthy"
  docker compose version >/dev/null 2>&1 || fatal "docker compose v2 不可用"
  ok "docker compose v2 OK"
}

check_env() {
  # 必要 env(沒設就停)。CSP_SECRET_KEY / SECRET_KEY 擇一即可
  # (infra/compose/platform.yml 內 csp service 看的是 CSP_SECRET_KEY)。
  local required=(CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY)
  local missing=()
  for v in "${required[@]}"; do
    if [[ -z "${!v:-}" ]]; then
      missing+=("$v")
    elif [[ "${!v}" =~ (changeme|placeholder|example) ]]; then
      err "$v 看起來是 dev/sample 值: '${!v:0:30}...' — prod 部署請換成真正的 secret"
      missing+=("$v")
    fi
  done
  # CSP_SECRET_KEY / SECRET_KEY 擇一
  if [[ -z "${CSP_SECRET_KEY:-}" ]] && [[ -z "${SECRET_KEY:-}" ]]; then
    missing+=(CSP_SECRET_KEY)
  fi
  if (( ${#missing[@]} > 0 )); then
    fatal "缺少或 dev 值的必要 env: ${missing[*]}
       export 它們後重跑,或載入你的 prod .env:
         set -a; source /path/to/prod.env; set +a
         bash infra/deployment/scripts/deploy-prod.sh"
  fi
  # ANILA_ALLOW_DEV_SECRET=1 會把 startup_security 的硬擋降成 log warning
  # (含 P2.7 稽核帳防竄改)。prod 部署必須拒絕——本機開發逃生口不可進 .15。
  # 對齊 Python ``.strip() == "1"``;同時掃 repo-root ./.env——docker compose
  # 會讀那個檔,光看 shell env 會漏掉「shell 乾淨但 .env 帶 =1」的半盲路徑。
  local _allow_dev="${ANILA_ALLOW_DEV_SECRET:-}"
  _allow_dev="${_allow_dev#"${_allow_dev%%[![:space:]]*}"}"
  _allow_dev="${_allow_dev%"${_allow_dev##*[![:space:]]}"}"
  if [[ "$_allow_dev" == "1" ]] || \
     { [[ -f .env ]] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?ANILA_ALLOW_DEV_SECRET[[:space:]]*=[[:space:]]*['"'"'"]?1['"'"'"]?[[:space:]]*$' .env; }; then
    fatal "ANILA_ALLOW_DEV_SECRET=1 禁止用於 prod 部署:此旗標會關閉 startup_security 對 dev 預設值與 P2.7 稽核帳防竄改的硬擋(只剩 log warning)。請在 shell 與 .env 都設 ANILA_ALLOW_DEV_SECRET=0 後重跑。"
  fi
  # Slice 6 旗標分域:少了 ANILA_ENV=production,「模型 http fail-closed」硬規則
  # 不會生效(url_guard 以此判定 production)。不擋部署,但大聲提醒。
  if [[ "${ANILA_ENV:-}" != "production" && "${ANILA_ENV:-}" != "prod" ]]; then
    warn "ANILA_ENV 未設為 production — 正式模型 http fail-closed 守衛不會啟用;請在 .env 設 ANILA_ENV=production"
  fi
  ok "必要 env 都已設且非 dev 值"
}

check_models_stack() {
  # ── 遠端模型模式 (內網拓撲:模型在 10.53.100.12,平台在 10.53.100.15) ──
  # ANILA_REMOTE_MODELS=1 → 本機沒有 models stack:跳過本機 container
  # health,改 curl .env 給的 *_BASE_URL。compose 仍引用 external network
  # anila-models-net (缺了 up 會失敗),這裡順手建一個空的。
  if [[ "${ANILA_REMOTE_MODELS:-0}" == "1" ]]; then
    if ! docker network inspect anila-models-net >/dev/null 2>&1; then
      log "遠端模型模式:建立空的 anila-models-net (compose external 引用需要)"
      docker network create anila-models-net >/dev/null
    fi
    ok "anila-models-net network 存在 (remote-models mode)"

    local probes=()
    [[ -n "${GEMMA4_BASE_URL:-}" ]] && probes+=("gemma4|${GEMMA4_BASE_URL}")
    [[ -n "${LOCAL_LLM_BASE_URL:-}" ]] && probes+=("local-llm|${LOCAL_LLM_BASE_URL}")
    [[ -n "${LOCAL_EMBEDDING_BASE_URL:-}" ]] && probes+=("embedding|${LOCAL_EMBEDDING_BASE_URL}")
    if (( ${#probes[@]} == 0 )); then
      warn "遠端模型模式但 GEMMA4/LOCAL_LLM/LOCAL_EMBEDDING_BASE_URL 都沒設 — chat/embedding 會打不到模型"
      return
    fi
    # gateway 的 /v1 要 Bearer key (My-OpenAI-Frontend);有設就帶上,
    # 沒設時 401 也會被當探測失敗 — 屬正確行為 (key 沒發就是還沒就緒)。
    local auth_args=()
    [[ -n "${MODEL_GATEWAY_API_KEY:-}" ]] && auth_args=(-H "Authorization: Bearer ${MODEL_GATEWAY_API_KEY}")
    local degraded=0 p name url
    for p in "${probes[@]}"; do
      name="${p%%|*}"; url="${p#*|}"
      # vLLM / OpenAI-compatible server 都有 /v1/models;5s timeout 夠內網用。
      # -k:這裡只測可達性,內部 CA 的信任鏈由容器內 SSL_CERT_FILE 處理
      # (見 .env 的 ANILA_MODEL_CA_FILE),host 端 curl 不用裝 CA。
      if curl -sfk -m 5 ${auth_args[@]+"${auth_args[@]}"} "${url%/}/v1/models" >/dev/null 2>&1; then
        ok "$name: $url 可達"
      else
        warn "$name: $url 探測失敗 (服務沒起 / port 不對 / 防火牆擋)"
        degraded=1
      fi
    done
    (( degraded > 0 )) && warn "部分遠端模型不可達,csp 仍可起來但對應功能會失敗"
    return
  fi

  if ! docker network inspect anila-models-net >/dev/null 2>&1; then
    err "anila-models-net network 不存在"
    fatal "請先起模型 stack:
       bash infra/deployment/intranet/model-serve.sh up trial
       (確認 gemma4 / flux2-dev / flux2-dev-agent / nv-embed-proxy 都 healthy)
       模型在別台主機的內網部署 → export ANILA_REMOTE_MODELS=1 重跑"
  fi
  ok "anila-models-net network 存在"

  # 列必要的 model service,讓 user 看到 health
  local need=(anila-model-gemma4 anila-model-nv-embed-proxy anila-model-flux2-dev-agent)
  local degraded=0
  for c in "${need[@]}"; do
    local status
    status=$(docker inspect "$c" --format '{{.State.Health.Status}}' 2>/dev/null || echo "missing")
    case "$status" in
      healthy)   ok "$c: healthy" ;;
      starting)  warn "$c: starting (尚未就緒)" ; degraded=1 ;;
      unhealthy) warn "$c: unhealthy(csp 對它的依賴可能 degraded)" ; degraded=1 ;;
      missing)   err "$c: 沒 running"; degraded=1 ;;
      *)         warn "$c: $status"; degraded=1 ;;
    esac
  done
  if (( degraded > 0 )); then
    warn "部分模型服務未就緒,csp 仍可起來但 chat/embedding 會失敗"
  fi
}

check_dirs() {
  # share/pki:內網模型 https 的內部 CA PEM 放置處 (csp 掛 /etc/anila/pki)。
  local dirs=(share/uploads/flux share/pki)
  for d in "${dirs[@]}"; do
    if [[ ! -d "$d" ]]; then
      log "建立缺漏目錄: $d"
      mkdir -p "$d"
    fi
    ok "$d/ 存在"
  done
}

# ── Subcommand: preflight ──────────────────────────────────────────────────
cmd_preflight() {
  section "ANILA Prod Deploy — Pre-flight checks"
  check_branch
  check_docker
  check_env
  check_dirs
  check_models_stack
  log "Pre-flight 全部通過"
}

# JWT 簽章金鑰：prod 模式 ALLOW_AUTO_KEYGEN=false 不自動生 → 缺這把 csp 的
# /.well-known/jwks.json 回 500、登入發不了 access token、anila-studio crash-loop。
# compose (infra/compose/platform.yml) 以 ./secrets mount 進 csp /app/secrets；compose up 前確保存在。
# (一條龍 intranet-deploy.sh 也有同款 [4b] 步驟;走 deploy-prod.sh 這條也補上。)
ensure_jwt_keypair() {
  mkdir -p -m 700 secrets   # secrets 目錄不可 world-listable
  if [[ ! -f secrets/jwt-private.pem || ! -f secrets/jwt-public.pem ]]; then
    # umask 077 子 shell:私鑰「建立當下」即 0600,消除 chmod 前的 TOCTOU 暴露窗。
    (
      umask 077
      openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out secrets/jwt-private.pem
      openssl pkey -in secrets/jwt-private.pem -pubout -out secrets/jwt-public.pem
    )
    chmod 600 secrets/jwt-private.pem
    chmod 644 secrets/jwt-public.pem   # 公鑰可讀
    echo "✓ 已產生 JWT 簽章 keypair (secrets/jwt-{private,public}.pem)"
  fi
}

# ── Subcommand: deploy ─────────────────────────────────────────────────────
cmd_deploy() {
  cmd_preflight

  section "Build images (csp / router / ingestion-worker / pptx-renderer / anila-studio / anilalm / anila-ui)"
  docker compose build

  section "JWT 簽章金鑰"
  ensure_jwt_keypair

  section "Bring up the stack"
  docker compose up -d

  cmd_wait_healthy
  reload_nginx
  cmd_verify
}


# ── nginx 上游 IP 重新解析 ────────────────────────────────────────────────────
# csp / router / anila-studio 走 `upstream` 區塊(有 keepalive,對熱路徑重要),
# 而 nginx OSS 對 upstream 區塊裡的 server 只在載入設定時解析一次 DNS 就釘死。
# 任何一次 recreate 都會讓那個服務換到新的容器 IP,nginx 卻繼續打舊 IP →
# **全站 502,但所有容器 healthy**(2026-07-30 實際中招:/api/auth/me 502 而
# /anila/ 200,健康檢查完全看不出來)。
#
# 不改成 variable proxy_pass 是刻意的:那會失去對 csp 的 keepalive,
# 每個請求多一次 TCP 握手,而那是使用者每則訊息都要走的路徑。
# 換成部署後 reload 一次——成本是零,前提是不能忘,所以寫進腳本而不是寫進文件。
reload_nginx() {
  if docker ps --format '{{.Names}}' | grep -q '^anila-nginx$'; then
    docker exec anila-nginx nginx -t >/dev/null 2>&1 \
      && docker exec anila-nginx nginx -s reload >/dev/null 2>&1 \
      && ok "nginx 已 reload(重新解析上游 IP)" \
      || warn "nginx reload 失敗——若出現 502 但容器 healthy,手動跑 docker exec anila-nginx nginx -s reload"
  fi
}

# ── Subcommand: up / down / restart ────────────────────────────────────────
cmd_up() {
  check_branch; check_docker; check_env
  ensure_jwt_keypair
  section "docker compose up -d"
  docker compose up -d
  cmd_wait_healthy
  reload_nginx
}

cmd_down() {
  check_docker
  section "docker compose down (保留 named volumes,db 資料不丟)"
  docker compose down
  ok "stack 已停"
}

cmd_restart() {
  cmd_down
  cmd_up
}

# ── Subcommand: rebuild <service> ──────────────────────────────────────────
cmd_rebuild() {
  local svc="${1:-}"
  [[ -z "$svc" ]] && fatal "usage: $0 rebuild <service>  (e.g. csp / anila-studio / anila-ui)"
  check_docker
  check_env
  section "Rebuild + restart: $svc"
  docker compose build "$svc"
  docker compose up -d "$svc"
  log "等 15 秒 healthcheck..."
  sleep 15
  docker compose ps "$svc"
}

# ── Subcommand: status ─────────────────────────────────────────────────────
cmd_status() {
  section "ANILA stack status"
  docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Image}}"
  echo
  log "模型 stack (anila-models project):"
  docker compose -p anila-models ps --format "table {{.Service}}\t{{.Status}}" 2>/dev/null || \
    warn "anila-models project 沒 running"
}

# ── Subcommand: logs <service> ─────────────────────────────────────────────
cmd_logs() {
  local svc="${1:-}"
  [[ -z "$svc" ]] && fatal "usage: $0 logs <service>"
  docker compose logs -f --tail=100 "$svc"
}

# ── Subcommand: wait healthy + verify ──────────────────────────────────────
cmd_wait_healthy() {
  section "等所有 service healthy(最多 5 分鐘)"
  local deadline=$(( $(date +%s) + 300 ))
  local services=(csp-db redis pptx-renderer csp router anilalm anila-ui anila-studio nginx)
  while (( $(date +%s) < deadline )); do
    local pending=()
    for s in "${services[@]}"; do
      local status
      status=$(docker compose ps "$s" --format '{{.Status}}' 2>/dev/null || echo "")
      if [[ -z "$status" ]] || [[ "$status" != *"healthy"* ]]; then
        if [[ "$status" == *"unhealthy"* ]] || [[ "$status" == *"Restarting"* ]]; then
          err "$s: $status"
          warn "看 logs 找原因: bash $0 logs $s"
          return 1
        fi
        pending+=("$s")
      fi
    done
    if (( ${#pending[@]} == 0 )); then
      ok "全部 healthy"
      return 0
    fi
    log "等待中:${pending[*]}"
    sleep 5
  done
  err "5 分鐘內仍有 service 未 healthy"
  cmd_status
  return 1
}

cmd_verify() {
  section "Endpoint smoke test"
  # 試 nginx → csp /health
  local nginx_health
  nginx_health=$(curl -sk -o /dev/null -w "%{http_code}" \
    --max-time 5 https://localhost/health 2>&1 || echo "fail")
  if [[ "$nginx_health" == "200" ]]; then
    ok "https://localhost/health → 200"
  else
    warn "https://localhost/health → $nginx_health (TLS cert 可能要重簽,bash infra/deployment/scripts/reissue-tls-cert.sh)"
  fi

  # 試 csp directly (cluster-internal,從 nginx container 出)
  if docker compose exec -T csp curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    ok "csp /health (internal) → 200"
  else
    warn "csp /health (internal) 失敗"
  fi

  # 試 anila-studio
  if docker compose exec -T anila-studio curl -sf http://localhost:8100/health >/dev/null 2>&1; then
    ok "anila-studio /health (internal) → 200"
  else
    warn "anila-studio /health (internal) 失敗"
  fi

  # 試 /api/auth/revocations(anila-studio 的 cold-start dep)
  local revoke_check
  revoke_check=$(docker compose exec -T anila-studio sh -c \
    "curl -sf -H 'X-CSP-Service-Token: '\$CSP_SERVICE_TOKEN \
     http://csp:8000/api/auth/revocations?since=2026-01-01T00:00:00Z \
     -o /dev/null -w '%{http_code}'" 2>&1 || echo "fail")
  if [[ "$revoke_check" == "200" ]]; then
    ok "csp /api/auth/revocations → 200 (anila-studio cold-start 通了)"
  else
    warn "csp /api/auth/revocations → $revoke_check"
  fi
}

# ── 顯示說明 ──────────────────────────────────────────────────────────────
cmd_help() {
  sed -n '/^# ====/,/^# ====/p' "$0" | head -50 | sed 's/^# \?//'
}

# ── Entrypoint ────────────────────────────────────────────────────────────
SUBCMD="${1:-deploy}"
shift || true

case "$SUBCMD" in
  preflight) cmd_preflight ;;
  deploy)    cmd_deploy ;;
  up)        cmd_up ;;
  down)      cmd_down ;;
  restart)   cmd_restart ;;
  rebuild)   cmd_rebuild "$@" ;;
  status)    cmd_status ;;
  logs)      cmd_logs "$@" ;;
  verify)    cmd_verify ;;
  wait)      cmd_wait_healthy ;;
  help|-h|--help) cmd_help ;;
  *)
    err "未知 subcommand: $SUBCMD"
    cmd_help
    exit 1
    ;;
esac
