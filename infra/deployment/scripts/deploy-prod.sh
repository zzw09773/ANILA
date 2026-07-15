#!/usr/bin/env bash
# ============================================================================
# deploy-prod.sh
# ----------------------------------------------------------------------------
# ANILA prod-intranet-card 正式部署腳本（Gate 1 F6 reviewed scope）
#
# 唯一接受的 prod branch:
#   - prod-intranet-card   中科院內網 + PKI 自然人憑證卡
# public/military 尚無 Gate 1 reviewed profile + image-lock lifecycle，不得混用。
#
# 用法:
#   git checkout prod-intranet-card && git pull
#   set -a; source /path/to/prod-intranet-card.env; set +a
#   bash infra/deployment/scripts/deploy-prod.sh [SUBCOMMAND]
#
# SUBCOMMAND:
#   preflight        只跑 pre-flight 檢查,不動 stack
#   tool-preflight   只驗 n8n/GitLab 資料版本護欄(一條龍部署共用)
#   deploy           preflight + locked no-build up + wait + verify（預設）
#   up               docker compose up -d --no-build + wait + fail-closed verify
#   down             docker compose down (保留 volumes,db 資料不丟)
#   restart          完整 preflight 通過後才 down + locked up
#   rebuild <svc>    正式 profile 一律拒絕；必須重出 air-gap bundle
#   status           顯示所有 service health
#   postconfigure    收斂帶狀態 upstream 工具的 runtime 安全姿態
#   logs <svc>       tail -f 單一 service logs
#   wait             等正式 service 全數 ready/healthy
#   verify           執行正式 endpoint、工具 owner 與 image pin 驗證
#   codeserver-up    明確啟用隔離的 browser IDE (developer-tools profile)
#   codeserver-down  停用並移除 browser IDE container
#   help             顯示這份說明
#
# 環境變數(必要,缺值 fail-loud):
#   CSP_SERVICE_TOKEN         legacy fleet service-to-service token
#   STUDIO_ARTIFACT_SERVICE_TOKEN dedicated named artifact-writer credential
#   INTERNAL_PLATFORM_API_KEY 內部 system worker API key,ingestion-worker 用
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
#   1. 現在 git branch 是 `prod-intranet-card`
#   2. Docker daemon running
#   3. docker compose v2 可用
#   4. anila-models-net network 已存在(模型 stack 先起來)
#   5. 模型服務(gemma4 / nv-embed-proxy)healthy；FLUX 預設停用
#   6. Gate 5 governance material 是 repo 外的唯讀正式部署輸入
#   7. 必要 env 已設且非 dev fallback
# ============================================================================
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

env_file_value() {
  if [[ -f .env ]]; then
    sed -n "s/^$1=//p" .env | head -1 | sed "s/^['\"]//;s/['\"]$//"
  fi
  return 0
}

deployment_profile() {
  printf '%s' "${ANILA_DEPLOYMENT_PROFILE:-$(env_file_value ANILA_DEPLOYMENT_PROFILE)}"
}

pilot_mode_value() {
  if [[ -v ANILA_PILOT_MODE ]]; then
    printf '%s' "$ANILA_PILOT_MODE"
  else
    printf '%s' "$(env_file_value ANILA_PILOT_MODE)"
  fi
}

FORMAL_POSTURE=normal
FORMAL_COMPOSE_ARGS=()
IMAGE_LOCK_POSTURE_ARGS=()

configure_formal_posture() {
  local requested
  requested="$(pilot_mode_value)"
  case "${requested,,}" in
    ""|false) FORMAL_POSTURE=normal ;;
    true) FORMAL_POSTURE=gate2-pilot ;;
    *) fatal "ANILA_PILOT_MODE 必須是明確的 true/false；目前為 '$requested'" ;;
  esac
  FORMAL_COMPOSE_ARGS=(--project-name anila-platform -f infra/compose/platform.yml)
  IMAGE_LOCK_POSTURE_ARGS=(--posture normal)
  if [[ "$FORMAL_POSTURE" == gate2-pilot ]]; then
    FORMAL_COMPOSE_ARGS+=(-f infra/compose/gate2-pilot.yml)
    IMAGE_LOCK_POSTURE_ARGS=(--posture gate2-pilot)
  fi
}

compose() {
  docker compose "${FORMAL_COMPOSE_ARGS[@]}" "$@"
}

is_gate2_pilot_posture() {
  [[ "$FORMAL_POSTURE" == gate2-pilot ]]
}

is_formal_card_profile() {
  case "${1:-$(deployment_profile)}" in
    prod-intranet-card|prod-intranet-card-breakglass) return 0 ;;
    *) return 1 ;;
  esac
}

# Must match infra/docker/csp.Dockerfile and intranet-deploy.sh.
CSP_RUNTIME_UID=10001
CSP_RUNTIME_GID=10001
CSP_RUNTIME_IMAGE="${ANILA_IMAGE_CSP:-$(env_file_value ANILA_IMAGE_CSP)}"
N8N_RUNTIME_IMAGE="${ANILA_IMAGE_N8N:-$(env_file_value ANILA_IMAGE_N8N)}"
GITLAB_RUNTIME_IMAGE="${ANILA_IMAGE_GITLAB:-$(env_file_value ANILA_IMAGE_GITLAB)}"
CSP_RUNTIME_IMAGE="${CSP_RUNTIME_IMAGE:-anila-platform-csp:latest}"
N8N_RUNTIME_IMAGE="${N8N_RUNTIME_IMAGE:-n8nio/n8n:2.29.10}"
GITLAB_RUNTIME_IMAGE="${GITLAB_RUNTIME_IMAGE:-gitlab/gitlab-ce:19.1.1-ce.0}"
IMAGE_LOCK_VERIFIER=infra/deployment/scripts/verify-compose-image-lock.py
GATE5_EGRESS_CHECKER=infra/policy/gate5/check_deployment_egress.py
TOOL_VERSION_MARKER=.anila-managed-image
COMPOSE_PROJECT=anila-platform

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

prepare_csp_runtime_mount() {
  local host_path="$1" dir_mode="$2"
  mkdir -p "$host_path"
  docker run --rm --pull never --user 0:0 --network none --read-only \
    --security-opt no-new-privileges \
    -v "$host_path:/mnt" \
    --entrypoint sh "$CSP_RUNTIME_IMAGE" \
    -c "chown $CSP_RUNTIME_UID:$CSP_RUNTIME_GID /mnt && chmod $dir_mode /mnt" \
    || fatal "無法準備 CSP non-root mount: $host_path"
}

# ── Pre-flight: 環境 ──────────────────────────────────────────────────────
# Gate 1 F6 的 executable posture/image lock 只審查 card 分支。
# 防呆:在其他 prod、main、dev-*、feature/* 上跑都必須被擋掉。
_PROD_BRANCHES=(prod-intranet-card)

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
    fatal "deployment identity mismatch: 請切到 prod-intranet-card 並同步正式 release"
  fi
  ok "git branch = $branch"
  ok "  特性: 中科院內網 + PKI 自然人憑證卡"
}

check_docker() {
  command -v docker >/dev/null || fatal "未找到 docker"
  docker info >/dev/null 2>&1 || fatal "docker daemon 沒在跑(或當前 user 無權限)"
  ok "docker daemon healthy"
  docker compose version >/dev/null 2>&1 || fatal "docker compose v2 不可用"
  ok "docker compose v2 OK"
}

check_compose_control_env() {
  local variable
  for variable in COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME \
    COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PATH_SEPARATOR; do
    [[ ! -v "$variable" ]] \
      || fatal "formal lifecycle 不接受 ambient $variable；請 unset 後重跑"
  done
}

check_formal_image_lock() {
  local profile branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  [[ "$branch" == "prod-intranet-card" ]] \
    || fatal "deployment identity mismatch: profile/image-lock lifecycle 只核准 prod-intranet-card branch（目前 $branch）"
  profile="$(deployment_profile)"
  [[ -n "$profile" ]] || fatal "ANILA_DEPLOYMENT_PROFILE 未宣告"
  if ! is_formal_card_profile "$profile"; then
    fatal "本 Gate 1 lifecycle 只核准 prod-intranet-card 與 prod-intranet-card-breakglass；目前為未審查 profile: $profile"
  fi
  command -v python3 >/dev/null 2>&1 \
    || fatal "formal image-lock verifier 需要 python3"
  python3 "$IMAGE_LOCK_VERIFIER" "${IMAGE_LOCK_POSTURE_ARGS[@]}" check-wiring \
    || fatal "Compose image lock wiring 不完整"
  python3 "$IMAGE_LOCK_VERIFIER" "${IMAGE_LOCK_POSTURE_ARGS[@]}" verify-env --env-file .env --inspect-docker \
    || fatal "正式 image content-ID lock 缺失或本機 image 不符"
  ok "formal Compose images 已鎖定 sha256 content IDs"
}

check_running_container_image_lock() {
  local optional_args=()
  if [[ -n "$(compose --profile developer-tools ps --status running -q codeserver 2>/dev/null || true)" ]]; then
    optional_args=(--include-optional)
  fi
  python3 "$IMAGE_LOCK_VERIFIER" "${IMAGE_LOCK_POSTURE_ARGS[@]}" verify-env --env-file .env \
    "${optional_args[@]}" --inspect-containers \
    || fatal "running container image content-ID read-back 失敗"
}

check_external_secret_paths() {
  command -v realpath >/dev/null 2>&1 \
    || fatal "未找到 realpath，無法驗證正式私鑰路徑是否在 repo 外"
  local repo_real state_lex state_real variable raw target_lex target_real leaf
  repo_real="$(realpath -m -- "$REPO_ROOT")"
  [[ -n "${ANILA_STATE_DIR:-}" ]] || fatal "ANILA_STATE_DIR 未設定"
  [[ "$ANILA_STATE_DIR" = /* ]] || fatal "ANILA_STATE_DIR 必須是絕對路徑"
  state_lex="$(realpath -ms -- "$ANILA_STATE_DIR")"
  state_real="$(realpath -m -- "$ANILA_STATE_DIR")"
  [[ "$state_lex" == "$state_real" ]] \
    || fatal "ANILA_STATE_DIR 不得包含 symlink component: $ANILA_STATE_DIR"
  case "$state_real" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib)
      fatal "ANILA_STATE_DIR 太寬或是系統目錄: $state_real" ;;
  esac
  case "$state_real" in "$repo_real"|"$repo_real"/*) fatal "ANILA_STATE_DIR 不得位於 repo 內: $state_real" ;; esac
  for variable in ANILA_SECRETS_DIR ANILA_TLS_CERTS_DIR; do
    raw="${!variable:-}"
    [[ -n "$raw" ]] || fatal "$variable 未設定"
    [[ "$raw" = /* ]] || fatal "$variable 必須是絕對路徑: $raw"
    target_lex="$(realpath -ms -- "$raw")"
    target_real="$(realpath -m -- "$raw")"
    [[ "$target_lex" == "$target_real" ]] \
      || fatal "$variable 不得包含 symlink component: $raw"
    [[ "$variable" = ANILA_SECRETS_DIR ]] && leaf=secrets || leaf=tls
    [[ "$target_real" == "$state_real/$leaf" ]] \
      || fatal "$variable 必須固定為 $state_real/$leaf (目前: $target_real)"
  done
  ok "JWT/TLS 私鑰路徑位於 repo 外"
}

check_env() {
  # 必要 env(沒設就停)。CSP_SECRET_KEY / SECRET_KEY 擇一即可
  # (infra/compose/platform.yml 內 csp service 看的是 CSP_SECRET_KEY)。
  local required=(CSP_SERVICE_TOKEN STUDIO_ARTIFACT_SERVICE_TOKEN STUDIO_RUNTIME_SERVICE_TOKEN STUDIO_JOB_ENVELOPE_HMAC_KEY INGESTION_QUEUE_HMAC_KEY INTERNAL_PLATFORM_API_KEY SITE_URL GITLAB_SSH_BIND_IP ANILA_ENV ANILA_DEPLOYMENT_PROFILE GATE5_MATERIAL_DIR ANILA_STATE_DIR ANILA_SECRETS_DIR ANILA_TLS_CERTS_DIR)
  local branch
  branch="$(git branch --show-current 2>/dev/null || true)"
  if [[ "$branch" == "prod-intranet-card" ]]; then
    required+=(N8N_HOST N8N_EDITOR_BASE_URL N8N_WEBHOOK_URL N8N_TLS_REJECT_UNAUTHORIZED N8N_OWNER_EMAIL N8N_OWNER_PASSWORD_HASH N8N_ENCRYPTION_KEY GITLAB_HOST GITLAB_ROOT_PASSWORD CODESERVER_HOST CODESERVER_PASSWORD)
  fi
  local missing=()
  for v in "${required[@]}"; do
    if [[ -z "${!v:-}" ]]; then
      missing+=("$v")
    elif [[ "${!v}" =~ (changeme|placeholder|example) ]] || [[ "${!v}" == \<*\> ]]; then
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
  [[ "${STUDIO_ARTIFACT_SERVICE_TOKEN:-}" == csk-* ]] \
    || fatal "STUDIO_ARTIFACT_SERVICE_TOKEN 必須是專用 csk- Service Client token"
  [[ "${STUDIO_RUNTIME_SERVICE_TOKEN:-}" == csk-* && "${STUDIO_RUNTIME_SERVICE_TOKEN}" != "${STUDIO_ARTIFACT_SERVICE_TOKEN}" ]] \
    || fatal "STUDIO_RUNTIME_SERVICE_TOKEN 必須是另一把專用 csk- Service Client token"
  (( ${#STUDIO_JOB_ENVELOPE_HMAC_KEY} >= 32 )) \
    || fatal "STUDIO_JOB_ENVELOPE_HMAC_KEY 長度必須 >= 32"
  (( ${#INGESTION_QUEUE_HMAC_KEY} >= 32 )) \
    || fatal "INGESTION_QUEUE_HMAC_KEY 長度必須 >= 32"
  if [[ "$branch" == "prod-intranet-card" ]]; then
    [[ "${N8N_HOST:-}" == "n8n.ai.ncsist.org.tw" ]] \
      || fatal "prod-intranet-card 的 N8N_HOST 必須是 n8n.ai.ncsist.org.tw"
    [[ "${N8N_EDITOR_BASE_URL:-}" == "https://n8n.ai.ncsist.org.tw/" ]] \
      || fatal "N8N_EDITOR_BASE_URL 必須是 https://n8n.ai.ncsist.org.tw/"
    [[ "${N8N_WEBHOOK_URL:-}" == "https://n8n.ai.ncsist.org.tw/" ]] \
      || fatal "N8N_WEBHOOK_URL 必須是 https://n8n.ai.ncsist.org.tw/ (ingress 仍由 nginx 封鎖)"
    [[ "${N8N_TLS_REJECT_UNAUTHORIZED:-}" == "1" ]] \
      || fatal "N8N_TLS_REJECT_UNAUTHORIZED 必須是 1"
    [[ "${N8N_NODE_FUNCTION_ALLOW_EXTERNAL:-}" != *"*"* ]] \
      || fatal "N8N_NODE_FUNCTION_ALLOW_EXTERNAL 不得使用 wildcard"
    [[ "${N8N_NODE_FUNCTION_ALLOW_BUILTIN:-}" != *"*"* ]] \
      || fatal "N8N_NODE_FUNCTION_ALLOW_BUILTIN 不得使用 wildcard"
    [[ "${GITLAB_HOST:-}" == "gitlab.ai.ncsist.org.tw" ]] \
      || fatal "prod-intranet-card 的 GITLAB_HOST 必須是 gitlab.ai.ncsist.org.tw"
    [[ "${CODESERVER_HOST:-}" == "code.ai.ncsist.org.tw" ]] \
      || fatal "prod-intranet-card 的 CODESERVER_HOST 必須是 code.ai.ncsist.org.tw"
    [[ "${N8N_OWNER_EMAIL:-}" =~ ^[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)*ncsist\.org\.tw$ ]] \
      || fatal "N8N_OWNER_EMAIL 必須是正式 ncsist.org.tw 信箱"
    [[ "${N8N_OWNER_PASSWORD_HASH:-}" =~ ^\$2[aby]\$([0-9]{2})\$[./A-Za-z0-9]{53}$ ]] \
      || fatal "N8N_OWNER_PASSWORD_HASH 必須是完整 bcrypt hash"
    (( 10#${BASH_REMATCH[1]} >= 12 )) || fatal "N8N_OWNER_PASSWORD_HASH bcrypt cost 必須 >= 12"
    (( ${#N8N_ENCRYPTION_KEY} >= 32 )) || fatal "N8N_ENCRYPTION_KEY 長度必須 >= 32"
    (( ${#GITLAB_ROOT_PASSWORD} >= 16 )) || fatal "GITLAB_ROOT_PASSWORD 長度必須 >= 16"
    (( ${#CODESERVER_PASSWORD} >= 16 )) || fatal "CODESERVER_PASSWORD 長度必須 >= 16"
  fi
  if [[ "${ANILA_ENV:-}" != "production" && "${ANILA_ENV:-}" != "prod" ]]; then
    fatal "ANILA_ENV 必須是 production/prod，否則正式模型 HTTP fail-closed 守衛不會啟用"
  fi
  check_external_secret_paths
  command -v realpath >/dev/null 2>&1 \
    || fatal "未找到 realpath，無法驗證 Gate 5 material 路徑"
  local material_lex material_real repo_real
  [[ "$GATE5_MATERIAL_DIR" = /* ]] || fatal "GATE5_MATERIAL_DIR 必須是絕對路徑"
  repo_real="$(realpath -m -- "$REPO_ROOT")"
  material_lex="$(realpath -ms -- "$GATE5_MATERIAL_DIR")"
  material_real="$(realpath -m -- "$GATE5_MATERIAL_DIR")"
  [[ "$material_lex" == "$material_real" ]] \
    || fatal "GATE5_MATERIAL_DIR 不得包含 symlink component: $GATE5_MATERIAL_DIR"
  case "$material_real" in
    "$repo_real"|"$repo_real"/*) fatal "GATE5_MATERIAL_DIR 不得位於 repo 內: $material_real" ;;
  esac
  ok "Gate 5 governance material 路徑位於 repo 外"
  ok "必要 env 都已設且非 dev 值"
}

check_gate5_egress_policy() {
  command -v python3 >/dev/null 2>&1 \
    || fatal "Gate 5 deployment egress checker 需要 python3"
  [[ -f "$GATE5_EGRESS_CHECKER" ]] \
    || fatal "找不到 Gate 5 deployment egress checker: $GATE5_EGRESS_CHECKER"

  local tmp platform_json models_json profile rc
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/anila-gate5-egress.XXXXXX")" \
    || fatal "無法建立 Gate 5 Compose read-back 暫存目錄"
  platform_json="$tmp/platform.json"
  models_json="$tmp/models.json"
  profile="$(deployment_profile)"
  if ! compose config --format json >"$platform_json"; then
    rm -rf -- "$tmp"
    fatal "無法解析 formal platform resolved Compose"
  fi
  local model_compose_args=( -p anila-models -f infra/models/docker-compose.yml )
  if [[ "${GATE5_FLUX_LEGAL_APPROVED:-false}" == "true" || "${GATE5_FLUX_LEGAL_APPROVED:-0}" == "1" ]]; then
    # A legal-approved FLUX posture is meaningful only when the resolved model
    # document was rendered with the named profile.  The egress checker then
    # performs the full service/label/network/binding checks.
    model_compose_args+=(--profile flux-approved)
  fi
  if ! docker compose "${model_compose_args[@]}" config --format json >"$models_json"; then
    rm -rf -- "$tmp"
    fatal "無法解析 formal model-stack resolved Compose"
  fi
  set +e
  python3 "$GATE5_EGRESS_CHECKER" \
    --profile "$profile" \
    --repo-root "$REPO_ROOT" \
    --compose-json "$platform_json" \
    --compose-json "$models_json"
  rc=$?
  set -e
  rm -rf -- "$tmp"
  (( rc == 0 )) || fatal "Gate 5 formal deployment egress/governance policy 失敗"
  ok "Gate 5 formal deployment egress/governance policy 通過"
}

model_network_recreate_hint() {
  cat >&2 <<'EOF'
安全重建指令（僅限先確認 network 沒有任何 attached containers）：
  docker network inspect anila-models-net --format '{{json .Containers}}'
  docker network rm anila-models-net
  docker network create --driver bridge --internal anila-models-net
EOF
}

check_models_network_internal() {
  if ! docker network inspect anila-models-net >/dev/null 2>&1; then
    fatal "anila-models-net network 不存在；先執行 docker network create --driver bridge --internal anila-models-net，再重跑"
  fi
  local internal driver containers
  internal="$(docker network inspect anila-models-net --format '{{.Internal}}' 2>/dev/null || true)"
  driver="$(docker network inspect anila-models-net --format '{{.Driver}}' 2>/dev/null || echo unknown)"
  if [[ "$internal" != "true" || "$driver" != "bridge" ]]; then
    containers="$(docker network inspect anila-models-net --format '{{json .Containers}}' 2>/dev/null || echo unknown)"
    err "anila-models-net 必須是 Docker internal bridge (Internal=true, Driver=bridge); actual Internal=${internal:-missing} Driver=$driver Containers=$containers"
    model_network_recreate_hint
    fatal "拒絕使用既有錯誤 network；不會自動刪除任何 attached-container network"
  fi
  ok "anila-models-net = internal bridge (Internal=true)"
}

check_flux_runtime_profile() {
  local approved="${GATE5_FLUX_LEGAL_APPROVED:-false}" service cid
  local flux_services=(anila-model-flux2-dev anila-model-flux2-dev-agent)
  if [[ "$approved" == "true" || "$approved" == "1" ]]; then
    for service in "${flux_services[@]}"; do
      cid="$(docker ps -q --filter "name=^/${service}$" 2>/dev/null || true)"
      [[ -n "$cid" ]] || fatal "GATE5_FLUX_LEGAL_APPROVED=true 但 FLUX container 未 running: $service"
    done
    ok "legal-approved FLUX profile 的兩個 container 都在 running"
  else
    for service in "${flux_services[@]}"; do
      cid="$(docker ps -q --filter "name=^/${service}$" 2>/dev/null || true)"
      [[ -z "$cid" ]] || fatal "FLUX container $service 正在 running，但 GATE5_FLUX_LEGAL_APPROVED 未核准；拒絕繼續"
    done
    ok "FLUX disabled posture：沒有 running FLUX container"
  fi
}

check_models_stack() {
  # ── 遠端模型模式 (內網拓撲:模型在 10.53.100.12,平台在 10.53.100.15) ──
  # ANILA_REMOTE_MODELS=1 → 本機沒有 models stack:跳過本機 container
  # health,改 curl .env 給的 *_BASE_URL。compose 仍引用 external network
  # anila-models-net (缺了 up 會失敗),這裡順手建一個空的。
  if [[ "${ANILA_REMOTE_MODELS:-0}" == "1" ]]; then
    if ! docker network inspect anila-models-net >/dev/null 2>&1; then
      log "遠端模型模式:建立空的 anila-models-net (compose external 引用需要)"
      docker network create --driver bridge --internal anila-models-net >/dev/null
    fi
    check_models_network_internal
    check_flux_runtime_profile

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
       (確認 gemma4 / nv-embed-proxy 都 healthy；FLUX 需另有法務核准 profile)
       模型在別台主機的內網部署 → export ANILA_REMOTE_MODELS=1 重跑"
  fi
  check_models_network_internal
  check_flux_runtime_profile

  # 列必要的 model service,讓 user 看到 health
  local need=(anila-model-gemma4 anila-model-nv-embed-proxy)
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

tool_upgrade_guidance() {
  local service="$1" actual="$2" expected="$3"
  case "$service" in
    n8n)
      printf '%s\n' \
        "n8n 資料不可從 $actual 由正常部署直接跳到 $expected。" \
        "先做離線備份，依官方 v2 migration guide 在維護窗口完成分段升級，" \
        "確認 workflow/credential 可解密，再讓目標 image 容器成功啟動並執行 verify。" >&2
      ;;
    gitlab)
      printf '%s\n' \
        "GitLab 資料不可從 $actual 由正常部署直接跳到 $expected。" \
        "先做完整 backup，依官方 required upgrade stops 逐站升級；16.10.x 起至少要經" \
        "16.11.10 → 17.3.7 → 17.5.5 → 17.8.7 → 17.11.7，再按官方路徑進到 18/19。" \
        "每站完成 background migrations 與健康驗證後才可繼續。" >&2
      ;;
  esac
}

check_one_tool_upgrade() {
  local service="$1" expected="$2" volume_label="$3"
  local cid actual volumes volume marker
  cid="$(compose ps -a -q "$service" 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    actual="$(docker inspect "$cid" --format '{{.Config.Image}}' 2>/dev/null || true)"
    if [[ "$actual" != "$expected" ]]; then
      tool_upgrade_guidance "$service" "${actual:-unknown}" "$expected"
      fatal "$service image 版本護欄拒絕啟動"
    fi
    ok "$service 現有 container image = $expected"
  fi

  volumes="$(docker volume ls -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter "label=com.docker.compose.volume=$volume_label" 2>/dev/null || true)"
  [[ -n "$volumes" ]] || { ok "$service 無既有資料卷 (fresh install)"; return 0; }
  if [[ "$(printf '%s\n' "$volumes" | sed '/^$/d' | wc -l | tr -d ' ')" != "1" ]]; then
    fatal "$service 找到多個受管資料卷，請先人工盤點: $volumes"
  fi
  volume="$(printf '%s\n' "$volumes" | head -1)"
  marker="$(docker run --rm --pull never --network none --read-only \
    -v "$volume:/state:ro" --entrypoint sh "$expected" \
    -c 'cat "/state/$1" 2>/dev/null' _ "$TOOL_VERSION_MARKER" \
    2>/dev/null || true)"
  if [[ "$marker" != "$expected" ]]; then
    tool_upgrade_guidance "$service" "unverified-volume:$volume" "$expected"
    fatal "$service 有既有資料卷但沒有通過目標版本驗證標記"
  fi
  ok "$service 資料卷已由 $expected 驗證"
}

check_tool_upgrade_safety() {
  section "n8n / GitLab data-version guard"
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" && "$COMPOSE_PROJECT_NAME" != "$COMPOSE_PROJECT" ]]; then
    fatal "COMPOSE_PROJECT_NAME 必須維持 $COMPOSE_PROJECT，否則無法可靠識別正式工具資料卷"
  fi
  local gitlab_config_volumes gitlab_data_volumes
  gitlab_config_volumes="$(docker volume ls -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter "label=com.docker.compose.volume=gitlab_config" 2>/dev/null || true)"
  gitlab_data_volumes="$(docker volume ls -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter "label=com.docker.compose.volume=gitlab_data" 2>/dev/null || true)"
  if { [[ -n "$gitlab_config_volumes" ]] && [[ -z "$gitlab_data_volumes" ]]; } || \
     { [[ -z "$gitlab_config_volumes" ]] && [[ -n "$gitlab_data_volumes" ]]; }; then
    fatal "GitLab config/data volume 只剩一側；拒絕把 partial restore 當 fresh install"
  fi
  check_one_tool_upgrade n8n "$N8N_RUNTIME_IMAGE" n8n_data
  check_one_tool_upgrade gitlab "$GITLAB_RUNTIME_IMAGE" gitlab_data
}

mark_verified_tool_versions() {
  compose exec -T n8n sh -c \
    'umask 077; printf "%s\n" "$1" > "/home/node/.n8n/$2"' \
    _ "$N8N_RUNTIME_IMAGE" "$TOOL_VERSION_MARKER" \
    || fatal "無法寫入 n8n 目標版本驗證標記"
  compose exec -T gitlab sh -c \
    'umask 077; printf "%s\n" "$1" > "/var/opt/gitlab/$2"' \
    _ "$GITLAB_RUNTIME_IMAGE" "$TOOL_VERSION_MARKER" \
    || fatal "無法寫入 GitLab 目標版本驗證標記"
  ok "n8n/GitLab 資料卷已標記為通過目標版本驗證"
}

# ── Subcommand: preflight ──────────────────────────────────────────────────
cmd_preflight() {
  section "ANILA Prod Deploy — Pre-flight checks"
  check_branch
  check_docker
  check_formal_image_lock
  check_env
  check_gate5_egress_policy
  check_dirs
  check_models_stack
  check_tool_upgrade_safety
  log "Pre-flight 全部通過"
}

# JWT 簽章金鑰：prod 模式 ALLOW_AUTO_KEYGEN=false 不自動生 → 缺這把 csp 的
# /.well-known/jwks.json 回 500、登入發不了 access token、anila-studio crash-loop。
# compose 以 repo 外 ANILA_SECRETS_DIR mount 進 csp /app/secrets；up 前確保存在。
# (一條龍 intranet-deploy.sh 也有同款 [4b] 步驟;走 deploy-prod.sh 這條也補上。)
ensure_jwt_keypair() {
  docker image inspect "$CSP_RUNTIME_IMAGE" >/dev/null 2>&1 \
    || fatal "缺少 $CSP_RUNTIME_IMAGE；先 build/load CSP image 才能準備 non-root mounts"

  [[ -n "${ANILA_SECRETS_DIR:-}" ]] || fatal "ANILA_SECRETS_DIR 未設定"
  prepare_csp_runtime_mount "$ANILA_SECRETS_DIR" 700
  prepare_csp_runtime_mount "$REPO_ROOT/share/uploads/ingestion" 700
  prepare_csp_runtime_mount "$ANILA_STATE_DIR/source-snapshots" 700
  # The host operator cannot traverse this UID 10001 + mode 0700 directory.
  # Validate/reuse/generate entirely as the CSP runtime user inside a bounded
  # one-shot container; partial, symlinked, malformed, or mismatched pairs fail.
  docker run --rm --pull never --network none --read-only \
    -v "$ANILA_SECRETS_DIR:/out" \
    --entrypoint python "$CSP_RUNTIME_IMAGE" \
    /app/scripts/generate-jwt-keypair.py --output-dir /out --ensure \
    || fatal "JWT keypair ensure 失敗"
  echo "✓ JWT 簽章 keypair 已驗證/就緒 ($ANILA_SECRETS_DIR/jwt-{private,public}.pem)"
  case "${GITLAB_SSH_BIND_IP:-}" in
    0.0.0.0|::) fatal "GITLAB_SSH_BIND_IP 不得綁所有介面,請填正式 LAN IP" ;;
  esac
  docker run --rm --pull never --user 0:0 --network none --read-only \
    -v "$ANILA_SECRETS_DIR:/mnt" \
    --entrypoint sh "$CSP_RUNTIME_IMAGE" \
    -c "chown $CSP_RUNTIME_UID:$CSP_RUNTIME_GID /mnt /mnt/jwt-private.pem /mnt/jwt-public.pem && chmod 700 /mnt && chmod 600 /mnt/jwt-private.pem && chmod 644 /mnt/jwt-public.pem" \
    || fatal "JWT keypair 權限收斂失敗"
}

validate_formal_identity() {
  check_branch
  check_docker
  check_formal_image_lock
}

validate_formal_up() {
  validate_formal_identity
  check_env
  check_gate5_egress_policy
  check_models_stack
  check_tool_upgrade_safety
  ensure_jwt_keypair
}

# ── Subcommand: deploy ─────────────────────────────────────────────────────
cmd_deploy() {
  cmd_preflight

  section "Use preloaded content-ID-locked images (formal profile; no build)"

  section "JWT 簽章金鑰"
  ensure_jwt_keypair

  section "Bring up the stack"
  compose up -d --no-build --pull never

  cmd_wait_healthy
  cmd_postconfigure --prevalidated
  cmd_verify --prevalidated
}

# ── Subcommand: up / down / restart ────────────────────────────────────────
cmd_up() {
  [[ "${1:-}" == "--prevalidated" ]] || validate_formal_up
  section "docker compose up -d --no-build --pull never"
  compose up -d --no-build --pull never
  cmd_wait_healthy
  cmd_postconfigure --prevalidated
  cmd_verify --prevalidated
}

cmd_down() {
  check_docker
  section "docker compose down (保留 named volumes,db 資料不丟)"
  compose down
  ok "stack 已停"
}

cmd_restart() {
  # Complete every non-destructive gate before taking the current stack down.
  # A missing lock/wrong profile must leave the running deployment untouched.
  validate_formal_up
  cmd_down
  cmd_up --prevalidated
}

# ── Opt-in developer tool: code-server ───────────────────────────────────
# 正式 stack 不自動啟動 code-server。其 workspace 固定掛
# share/codeserver-sandbox；開發者需先放入一份「獨立 clone」，不要把正式 repo
# root、.env、secrets 或憑證複製進去。
cmd_codeserver_up() {
  validate_formal_identity
  check_env
  is_gate2_pilot_posture \
    && fatal "Gate 2 pilot active set 禁止啟用 developer-tools/codeserver"
  python3 "$IMAGE_LOCK_VERIFIER" "${IMAGE_LOCK_POSTURE_ARGS[@]}" verify-env --env-file .env \
    --include-optional --inspect-docker \
    || fatal "developer-tools image 未鎖定或未載入"
  mkdir -p share/codeserver-sandbox
  chmod 700 share/codeserver-sandbox
  # Compose 的 service user 要跟建立 sandbox 的 operator 一致；Bash 的 UID
  # 預設不是 exported,不明確 export 會掉回 compose 的 1001 default。
  export UID
  GID="${GID:-$(id -g)}"; export GID
  if [[ -z "$(find share/codeserver-sandbox -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    warn "share/codeserver-sandbox 是空的 — 建議先從內網 GitLab clone 一份獨立 checkout"
  fi
  section "Enable code-server (developer-tools profile; isolated workspace)"
  compose --profile developer-tools up -d --no-build --pull never codeserver
  python3 "$IMAGE_LOCK_VERIFIER" "${IMAGE_LOCK_POSTURE_ARGS[@]}" verify-env --env-file .env \
    --include-optional --inspect-containers \
    || fatal "developer-tools container image read-back 失敗"
  compose --profile developer-tools ps codeserver
  ok "code-server: https://${CODESERVER_HOST:-code.ai.ncsist.org.tw}/ (原生密碼登入)"
}

cmd_codeserver_down() {
  check_docker
  section "Disable code-server"
  compose --profile developer-tools stop codeserver
  compose --profile developer-tools rm -f codeserver
  ok "code-server 已停用；sandbox 內容保留"
}

# ── Subcommand: rebuild <service> ──────────────────────────────────────────
cmd_rebuild() {
  local svc="${1:-}"
  [[ -z "$svc" ]] && fatal "usage: $0 rebuild <service>  (e.g. csp / anila-studio / anila-ui)"
  case "$svc" in
    n8n|gitlab)
      fatal "$svc 是帶狀態的 upstream 工具，不可用 rebuild 繞過分段升級護欄" ;;
  esac
  check_docker
  is_formal_card_profile \
    || fatal "本 Gate 1 lifecycle 不支援未審查 deployment profile: $(deployment_profile)"
  fatal "formal card profile 禁止在部署主機 rebuild；請在外網 build-and-export 產生新 bundle/content-ID lock，再以 intranet-deploy 套用"
}

# ── Subcommand: status ─────────────────────────────────────────────────────
cmd_status() {
  section "ANILA stack status"
  compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Image}}"
  echo
  log "模型 stack (anila-models project):"
  docker compose -p anila-models ps --format "table {{.Service}}\t{{.Status}}" 2>/dev/null || \
    warn "anila-models project 沒 running"
}

# ── Subcommand: logs <service> ─────────────────────────────────────────────
cmd_logs() {
  local svc="${1:-}"
  [[ -z "$svc" ]] && fatal "usage: $0 logs <service>"
  compose logs -f --tail=100 "$svc"
}

# ── Subcommand: wait healthy + verify ──────────────────────────────────────
cmd_wait_healthy() {
  local timeout="${ANILA_WAIT_TIMEOUT_SECONDS:-900}"
  [[ "$timeout" =~ ^[0-9]+$ ]] || fatal "ANILA_WAIT_TIMEOUT_SECONDS 必須是正整數"
  section "等正式 service ready/healthy(最多 ${timeout} 秒)"
  local deadline=$(( $(date +%s) + timeout ))
  local services=(csp-db redis pptx-renderer csp router anilalm anila-ui anila-studio nginx n8n gitlab)
  local running_services=(ingestion-worker)
  if is_gate2_pilot_posture; then
    services=(csp-db redis csp router anilalm anila-ui nginx n8n gitlab)
    running_services=()
  fi
  while true; do
    local pending=()
    for s in "${services[@]}"; do
      local status
      status=$(compose ps "$s" --format '{{.Status}}' 2>/dev/null || echo "")
      if [[ "$status" == *"(unhealthy)"* ]] || [[ "$status" == *"Restarting"* ]] || [[ "$status" == *"Exited"* ]]; then
          err "$s: $status"
          warn "看 logs 找原因: bash $0 logs $s"
          return 1
      fi
      if [[ -z "$status" ]] || [[ "$status" != *"(healthy)"* ]]; then
        pending+=("$s")
      fi
    done
    for s in "${running_services[@]}"; do
      local status
      status=$(compose ps "$s" --format '{{.Status}}' 2>/dev/null || echo "")
      if [[ -z "$status" ]] || [[ "$status" != Up* ]]; then
        if [[ "$status" == *"Restarting"* ]] || [[ "$status" == *"Exited"* ]] || [[ "$status" == *"Dead"* ]]; then
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
    local remaining=$(( deadline - $(date +%s) ))
    (( remaining > 0 )) || break
    if (( remaining < 5 )); then sleep "$remaining"; else sleep 5; fi
  done
  err "${timeout} 秒內仍有 service 未 ready/healthy"
  cmd_status
  return 1
}

verify_service_image() {
  local service="$1" expected="$2" cid actual
  cid="$(compose ps -a -q "$service" 2>/dev/null || true)"
  [[ -n "$cid" ]] || fatal "$service container 不存在"
  actual="$(docker inspect "$cid" --format '{{.Config.Image}}' 2>/dev/null || true)"
  [[ "$actual" == "$expected" ]] \
    || fatal "$service image pin 不符: expected=$expected actual=${actual:-unknown}"
  ok "$service image pin = $expected"
}
verify_curl() {
  local ca_file="${ANILA_VERIFY_CA_FILE:-}"
  if [[ -n "$ca_file" ]]; then
    [[ -f "$ca_file" && ! -L "$ca_file" ]] \
      || fatal "ANILA_VERIFY_CA_FILE 必須是 regular non-symlink CA bundle"
    curl --cacert "$ca_file" "$@"
  else
    curl "$@"
  fi
}


verify_https_code() {
  local label="$1" host="$2" path="$3" expected="$4" code
  code="$(verify_curl -s --max-time 15 --resolve "$host:443:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://$host$path" 2>/dev/null || true)"
  [[ "$code" == "$expected" ]] || fatal "$label: expected HTTP $expected, got ${code:-curl-failed}"
  ok "$label → $expected"
}

verify_https_port_code() {
  local label="$1" host="$2" port="$3" path="$4" expected="$5" code
  code="$(verify_curl -s --max-time 15 --resolve "$host:$port:127.0.0.1" \
    -o /dev/null -w '%{http_code}' "https://$host:$port$path" 2>/dev/null || true)"
  [[ "$code" == "$expected" ]] \
    || fatal "$label: expected HTTP $expected, got ${code:-curl-failed}"
  ok "$label → $expected"
}

verify_gate2_pilot_runtime_posture() {
  local main_host="$1" service config_json
  for service in ingestion-worker pptx-renderer anila-studio flux2-dev-agent codeserver; do
    if [[ -n "$(compose --profile gate2-data-admin --profile gate3-artifacts --profile developer-tools ps --status running -q "$service" 2>/dev/null || true)" ]]; then
      fatal "Gate 2 pilot 禁止的 service 仍在 running: $service"
    fi
  done

  config_json="$(compose config --format json)" \
    || fatal "無法 read-back Gate 2 pilot resolved Compose"
  printf '%s' "$config_json" | python3 -c '
import json, pathlib, sys
config = json.load(sys.stdin)
services = config.get("services", {})
csp = services.get("csp", {})
env = csp.get("environment", {})
expected = {
    "ANILA_PILOT_MODE": "true",
    "GATE2_PILOT_COMPOSE_POSTURE": "gate2-pilot-v1",
    "ENABLE_MEMORY": "false",
    "ENABLE_PILOT_PROMPT_GENERATOR": "false",
    "ENABLE_PILOT_INGESTION_JUDGE": "false",
    "ENABLE_PILOT_STUDIO_ARTIFACTS": "false",
}
for name, value in expected.items():
    if str(env.get(name, "")).lower() != value:
        raise SystemExit(f"resolved CSP pilot posture mismatch: {name}")
volumes = services.get("nginx", {}).get("volumes", [])
matches = [
    item for item in volumes
    if isinstance(item, dict)
    and item.get("target") == "/etc/nginx/snippets/router-posture.conf"
]
if len(matches) != 1:
    raise SystemExit("resolved nginx must have exactly one router posture mount")
mount = matches[0]
if pathlib.PurePath(str(mount.get("source", ""))).name != "router-disabled.conf":
    raise SystemExit("resolved nginx router posture is not disabled")
if mount.get("read_only") is not True:
    raise SystemExit("resolved nginx router posture mount is not read-only")
' || fatal "Gate 2 pilot resolved Compose posture read-back 失敗"

  verify_https_code "Gate 2 pilot 443 Router deny" "$main_host" /router/health 403
  verify_https_port_code "Gate 2 pilot 4443 Router deny" "$main_host" 4443 /router/health 403
  ok "Gate 2 pilot active service set、CSP marker 與 nginx Router deny 已 read-back"
}

verify_https_reachable() {
  local label="$1" host="$2" path="$3"
  verify_curl -sf --max-time 20 --resolve "$host:443:127.0.0.1" \
    -o /dev/null "https://$host$path" \
    || fatal "$label 無法經 nginx HTTPS origin 到達"
  ok "$label 可經 nginx HTTPS origin 到達"
}

verify_tool_host_rejected_on_ui_port() {
  local host="$1" code rc
  set +e
  code="$(verify_curl -s --max-time 10 --resolve "$host:4443:127.0.0.1" \
    --http1.1 \
    -o /dev/null -w '%{http_code}' "https://$host:4443/" 2>/dev/null)"
  rc=$?
  set -e
  # nginx 444 在 OpenSSL curl 回 CURLE_GOT_NOTHING(52)；Windows Schannel
  # 在 TLS 已建立後回 CURLE_RECV_ERROR(56)。兩者都必須沒有任何 HTTP
  # response；其他 transport failure 不視為有效 ingress rejection。
  [[ ( "$rc" == "52" || "$rc" == "56" ) && "$code" == "000" ]] \
    || fatal "tool host $host 在 CSP-bearing :4443 未被 nginx 444 拒絕 (rc=$rc http=${code:-none})"
  ok "tool host $host 在 :4443 被拒絕"
}

verify_tls_material() {
  local cert="$ANILA_TLS_CERTS_DIR/server.crt" key="$ANILA_TLS_CERTS_DIR/server.key"
  [[ -f "$cert" && ! -L "$cert" && -f "$key" && ! -L "$key" ]] \
    || fatal "TLS cert/key 必須是 repo 外的 regular non-symlink files"
  openssl x509 -in "$cert" -noout -checkend 86400 >/dev/null 2>&1 \
    || fatal "TLS certificate 已過期或 24 小時內到期"
  local host
  for host in anila.ai.ncsist.org.tw n8n.ai.ncsist.org.tw gitlab.ai.ncsist.org.tw code.ai.ncsist.org.tw; do
    openssl x509 -in "$cert" -noout -checkhost "$host" >/dev/null 2>&1 \
      || fatal "TLS certificate SAN 不包含 $host"
  done
  local cert_pub key_pub
  cert_pub="$(openssl x509 -in "$cert" -noout -pubkey 2>/dev/null | sha256sum | cut -d' ' -f1)"
  key_pub="$(openssl pkey -in "$key" -pubout 2>/dev/null | sha256sum | cut -d' ' -f1)"
  [[ -n "$cert_pub" && "$cert_pub" == "$key_pub" ]] || fatal "TLS cert 與 private key 不成對"
  ok "TLS expiry / SAN / key-pair 驗證通過"
}

# GitLab 的 self-signup 是資料庫 ApplicationSetting；只在
# GITLAB_OMNIBUS_CONFIG 寫 gitlab_signup_enabled 並不足以收斂既有或 fresh
# database。這個步驟可重複執行，且只改明確的 security posture；verify
# 保持純 read-back、不得偷偷修正失敗狀態。
cmd_postconfigure() {
  if [[ "${1:-}" != "--prevalidated" ]]; then
    validate_formal_identity
  fi
  check_running_container_image_lock
  section "Postconfigure stateful tool security posture"
  local gitlab_posture
  gitlab_posture="$(compose exec -T gitlab gitlab-rails runner \
    "s=ApplicationSetting.current or abort('application setting missing'); s.update!(signup_enabled: false) unless s.signup_enabled == false; s.reload; abort('signup still enabled') unless s.signup_enabled == false; puts 'ANILA_GITLAB_SIGNUP=false'" \
    2>&1)" || fatal "GitLab self-signup runtime posture 收斂失敗"
  [[ "$gitlab_posture" == *"ANILA_GITLAB_SIGNUP=false"* ]] \
    || fatal "GitLab postconfigure 未回報可驗證的 signup=false marker"
  ok "GitLab self-signup runtime posture 已收斂"
}

cmd_verify() {
  [[ "${1:-}" == "--prevalidated" ]] || validate_formal_identity
  section "Formal deployment verification (fail-closed)"
  local main_host="${ANILA_HOST:-anila.ai.ncsist.org.tw}"
  local n8n_host="${N8N_HOST:-n8n.ai.ncsist.org.tw}"
  local gitlab_host="${GITLAB_HOST:-gitlab.ai.ncsist.org.tw}"

  check_running_container_image_lock
  verify_tls_material
  verify_https_code "主平台 nginx /health" "$main_host" /health 200
  compose exec -T csp python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)" \
    >/dev/null 2>&1 || fatal "csp /ready (internal) 失敗"
  ok "csp /ready (internal) → 200"
  if is_gate2_pilot_posture; then
    verify_gate2_pilot_runtime_posture "$main_host"
  else
    compose exec -T anila-studio python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=5)" \
      >/dev/null 2>&1 || fatal "anila-studio /health (internal) 失敗"
    ok "anila-studio /health (internal) → 200"
    compose exec -T anila-studio python -c \
      "import os,urllib.request; r=urllib.request.Request('http://csp:8000/api/auth/revocations?since=2026-01-01T00:00:00Z', headers={'X-CSP-Service-Token': os.environ['CSP_SERVICE_TOKEN']}); urllib.request.urlopen(r, timeout=5)" \
      >/dev/null 2>&1 || fatal "csp /api/auth/revocations (studio cold-start dependency) 失敗"
    ok "csp /api/auth/revocations → 200"
  fi

  verify_service_image n8n "$N8N_RUNTIME_IMAGE"
  verify_service_image gitlab "$GITLAB_RUNTIME_IMAGE"
  compose exec -T n8n node -e \
    "fetch('http://127.0.0.1:5678/rest/settings').then(async r=>{if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();if(j?.data?.userManagement?.showSetupOnFirstLoad!==false)throw new Error('owner setup is still public')}).catch(e=>{console.error(e.message);process.exit(1)})" \
    >/dev/null 2>&1 || fatal "n8n env-managed owner 尚未收斂，fresh-install takeover 仍可能發生"
  ok "n8n owner setup 已關閉且由 env 管理"
  local gitlab_migration_list gitlab_pending_by_db gitlab_schema_status
  gitlab_migration_list="$(compose exec -T gitlab \
    gitlab-rake gitlab:background_migrations:list 2>&1)" \
    || fatal "GitLab 19 cross-database background migration list command 失敗"
  [[ "$gitlab_migration_list" == *"id"* && "$gitlab_migration_list" == *"status"* ]] \
    || fatal "GitLab background migration list 缺少預期表頭，拒絕假綠燈"
  if printf '%s\n' "$gitlab_migration_list" | awk -F'|' '
    $1 ~ /^[[:space:]]*(main|ci)_[0-9]+[[:space:]]*$/ {
      status=$4; gsub(/^[[:space:]]+|[[:space:]]+$/, "", status)
      if (status != "finished" && status != "finalized") bad=1
    }
    END { exit bad ? 0 : 1 }
  '; then
    fatal "GitLab main/ci 尚有未完成 background migration"
  fi
  gitlab_pending_by_db="$(compose exec -T gitlab gitlab-rails runner \
    "Gitlab::Database.database_base_models.each { |n,m| next unless Gitlab::Database.has_database?(n); c=m.connection; p=c.data_source_exists?('batched_background_migrations') ? c.select_value('SELECT count(*) FROM batched_background_migrations WHERE status NOT IN (3, 6)').to_i : 0; puts \"ANILA_BG #{n}=#{p}\" }" \
    2>&1)" || fatal "GitLab 無法逐 database 查 background migrations"
  [[ "$gitlab_pending_by_db" == *"ANILA_BG "* ]] \
    || fatal "GitLab 沒有回報任何 database background migration 狀態"
  if printf '%s\n' "$gitlab_pending_by_db" | grep -Eq '^ANILA_BG [^=]+=[1-9][0-9]*$'; then
    fatal "GitLab 至少一個 database 尚有 background migration"
  fi
  gitlab_schema_status="$(compose exec -T gitlab gitlab-rake db:migrate:status 2>&1)" \
    || fatal "GitLab schema migration status command 失敗"
  if printf '%s\n' "$gitlab_schema_status" | grep -Eq '^[[:space:]]*down[[:space:]]'; then
    fatal "GitLab 尚有 down schema migration"
  fi
  ok "GitLab main/ci background 與 schema migrations 全數完成"
  compose exec -T gitlab gitlab-rails runner \
    "s=ApplicationSetting.current; abort('signup enabled') unless s && s.signup_enabled == false; u=User.find_by_username('root'); abort('root admin missing') unless u && u.admin?" \
    >/dev/null 2>&1 || fatal "GitLab signup/root-admin runtime posture 驗證失敗"
  ok "GitLab self-signup 關閉且 root admin 已 bootstrap"

  verify_https_reachable "n8n origin" "$n8n_host" /rest/settings
  verify_curl -sf --max-time 20 --resolve "$gitlab_host:443:127.0.0.1" \
    "https://$gitlab_host/users/sign_in" 2>/dev/null | grep -qi gitlab \
    || fatal "GitLab origin 未回傳 GitLab sign-in page"
  ok "GitLab origin 回傳 GitLab sign-in page"
  local path
  for path in /webhook/gate0 /WEBHOOK/gate0 /webhook-test/gate0 /webhook-waiting/gate0 \
    /form/gate0 /FORM/gate0 /form-test/gate0 /form-waiting/gate0 \
    /mcp/gate0 /MCP-TEST/gate0 /mcp-server/http /MCP-SERVER/http \
    /mcp-oauth/register /OAUTH/token /.well-known/oauth-authorization-server \
    /.well-known/oauth-protected-resource/mcp/gate0; do
    verify_https_code "n8n unauthenticated ingress $path" "$n8n_host" "$path" 404
  done
  for path in /n8n /n8n/gate0 /gitlab /gitlab/gate0 /codeserver /codeserver/gate0; do
    verify_https_code "main-origin legacy tool path $path" "$main_host" "$path" 404
  done
  verify_tool_host_rejected_on_ui_port "$n8n_host"
  verify_tool_host_rejected_on_ui_port "$gitlab_host"
  verify_tool_host_rejected_on_ui_port "${CODESERVER_HOST:-code.ai.ncsist.org.tw}"

  mark_verified_tool_versions
  ok "正式部署驗證全部通過"
}

# ── 顯示說明 ──────────────────────────────────────────────────────────────
cmd_help() {
  sed -n '/^# ====/,/^# ====/p' "$0" | head -50 | sed 's/^# \?//'
}

# ── Entrypoint ────────────────────────────────────────────────────────────
SUBCMD="${1:-deploy}"
shift || true

check_compose_control_env
configure_formal_posture

case "$SUBCMD" in
  preflight) cmd_preflight ;;
  tool-preflight) validate_formal_identity; check_tool_upgrade_safety ;;
  deploy)    cmd_deploy ;;
  up)        cmd_up ;;
  down)      cmd_down ;;
  restart)   cmd_restart ;;
  rebuild)   cmd_rebuild "$@" ;;
  status)    cmd_status ;;
  logs)      cmd_logs "$@" ;;
  codeserver-up)   cmd_codeserver_up ;;
  codeserver-down) cmd_codeserver_down ;;
  verify)    cmd_verify ;;
  wait)      cmd_wait_healthy ;;
  help|-h|--help) cmd_help ;;
  postconfigure) cmd_postconfigure ;;
  *)
    err "未知 subcommand: $SUBCMD"
    cmd_help
    exit 1
    ;;
esac
