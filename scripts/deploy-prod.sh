#!/usr/bin/env bash
# ============================================================================
# deploy-prod.sh
# ----------------------------------------------------------------------------
# ANILA Prod 內網部署腳本(中科院內部署用)
#
# 用法:
#   bash scripts/deploy-prod.sh [SUBCOMMAND]
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

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
check_branch() {
  local branch
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  if [[ "$branch" != "prod" ]]; then
    err "目前在 '$branch' 分支,prod 部署必須切到 prod"
    fatal "請執行: git checkout prod && git pull origin prod"
  fi
  ok "git branch = prod"
}

check_docker() {
  command -v docker >/dev/null || fatal "未找到 docker"
  docker info >/dev/null 2>&1 || fatal "docker daemon 沒在跑(或當前 user 無權限)"
  ok "docker daemon healthy"
  docker compose version >/dev/null 2>&1 || fatal "docker compose v2 不可用"
  ok "docker compose v2 OK"
}

check_env() {
  # 必要 env(沒設就停)
  local required=(CSP_SERVICE_TOKEN INTERNAL_PLATFORM_API_KEY SECRET_KEY)
  local missing=()
  for v in "${required[@]}"; do
    if [[ -z "${!v:-}" ]]; then
      missing+=("$v")
    elif [[ "${!v}" =~ (dev|changeme|placeholder|example) ]]; then
      err "$v 看起來是 dev/sample 值: '${!v:0:30}...' — prod 部署請換成真正的 secret"
      missing+=("$v")
    fi
  done
  if (( ${#missing[@]} > 0 )); then
    fatal "缺少或 dev 值的必要 env: ${missing[*]}
       export 它們後重跑,或載入你的 prod .env:
         set -a; source /path/to/prod.env; set +a
         bash scripts/deploy-prod.sh"
  fi
  ok "必要 env 都已設且非 dev 值"
}

check_models_stack() {
  if ! docker network inspect anila-models-net >/dev/null 2>&1; then
    err "anila-models-net network 不存在"
    fatal "請先起模型 stack:
       cd models && docker compose up -d
       (確認 gemma4 / flux2-dev / flux2-dev-agent / nv-embed-proxy 都 healthy)"
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
  local dirs=(share/uploads/flux)
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

# ── Subcommand: deploy ─────────────────────────────────────────────────────
cmd_deploy() {
  cmd_preflight

  section "Build images (csp / router / ingestion-worker / pptx-renderer / anila-studio / anilalm / anila-ui)"
  docker compose build

  section "Bring up the stack"
  docker compose up -d

  cmd_wait_healthy
  cmd_verify
}

# ── Subcommand: up / down / restart ────────────────────────────────────────
cmd_up() {
  check_branch; check_docker; check_env
  section "docker compose up -d"
  docker compose up -d
  cmd_wait_healthy
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
    warn "https://localhost/health → $nginx_health (TLS cert 可能要重簽,bash scripts/reissue-tls-cert.sh)"
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
