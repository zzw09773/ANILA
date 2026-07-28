#!/usr/bin/env bash
# =============================================================================
# dev-up.sh — 冪等一鍵起 anila-platform-dev（compose.dev.yaml）
# =============================================================================
#
# 收斂新環境起站必經路徑：
#   anila-models-net → .env（從 .env.dev.example）→ compose up --build
#   → router-chain-bootstrap（缺 token 時）→ csp recreate 後重啟依賴
#   → 等 csp healthy + 全服務健康摘要
#
# 用法（repo 根）：
#   bash infra/deployment/scripts/dev-up.sh
#   bash infra/deployment/scripts/dev-up.sh --dry-run
#
# 冪等：對已運行的 stack 重跑應為無害（up 不強制 recreate；token 已填則跳過
# bootstrap；csp 未 recreate 則不 restart 依賴）。
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="${DEV_UP_COMPOSE_FILE:-compose.dev.yaml}"
ENV_FILE="$ROOT_DIR/.env"
ENV_EXAMPLE="$ROOT_DIR/.env.dev.example"
BOOTSTRAP_SCRIPT="$ROOT_DIR/infra/deployment/scripts/router-chain-bootstrap.sh"
CSP_WAIT_SECONDS="${DEV_UP_CSP_WAIT_SECONDS:-90}"
DRY_RUN=0

# shellcheck disable=SC1091
source "$ROOT_DIR/infra/deployment/scripts/ensure-models-network.sh"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: 未知參數 '$arg'（僅支援 --dry-run）" >&2
      exit 2
      ;;
  esac
done

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }
dry()  { printf 'DRY-RUN: %s\n' "$*"; }

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

# 從 .env 讀單一鍵（不 source；不印值）。缺鍵或空字串回傳空。
env_file_value() {
  local key="$1" line
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    if [[ "$line" == "$key="* ]]; then
      local val="${line#"$key="}"
      val="${val#$'\r'}"
      if [[ "$val" == \"*\" && "$val" == *\" ]]; then
        val="${val:1:${#val}-2}"
      elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
        val="${val:1:${#val}-2}"
      fi
      printf '%s' "$val"
      return 0
    fi
  done < "$ENV_FILE"
}

# ── 0. --dry-run：只印計畫 ───────────────────────────────────────────────
if [[ "$DRY_RUN" == "1" ]]; then
  log "dry-run 計畫（不執行任何變更）"
  dry "檢查 docker CLI / daemon 可用"
  dry "ensure anila-models-net（不存在則 docker network create --internal bridge）"
  if [[ ! -f "$ENV_FILE" ]]; then
    dry "cp .env.dev.example .env，提示必填項後退出（不繼續 up）"
  else
    dry "掃描 .env 的 <...> 占位符並警告"
    dry "確認 EMBEDDING_MODEL_FINGERPRINT_DEV 與 ANILA_DEV_TLS_CERTS_DIR 已填"
    dry "docker compose -f $COMPOSE_FILE up -d --build"
    token_preview="$(env_file_value ANILA_CSP_REGISTRY_SERVICE_TOKEN || true)"
    if [[ -z "${token_preview}" ]]; then
      dry "ANILA_CSP_REGISTRY_SERVICE_TOKEN 為空 → 跑 router-chain-bootstrap.sh"
      dry "docker compose -f $COMPOSE_FILE up -d --no-deps router csp"
    else
      dry "ANILA_CSP_REGISTRY_SERVICE_TOKEN 已設 → 跳過 bootstrap"
    fi
    dry "若 csp 本次被 (re)create → restart router anila-studio asr-gateway"
    dry "等待 csp healthy（上限 ${CSP_WAIT_SECONDS}s）後印全服務健康摘要；unhealthy → exit 1"
  fi
  exit 0
fi

# ── 1. 前置檢查 ──────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { err "需要 docker CLI"; exit 2; }
if ! docker info >/dev/null 2>&1; then
  err "docker daemon 不可用（docker info 失敗）"
  exit 2
fi
log "docker 可用"

log "確保 anila-models-net 存在"
ensure_models_network

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ ! -f "$ENV_EXAMPLE" ]]; then
    err "缺少 $ENV_EXAMPLE，無法建立 .env"
    exit 1
  fi
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  log "已複製 .env.dev.example → .env"
  cat <<'EOF'

請編輯 .env 填入下列必填項後再重跑本腳本（不代填 secrets）：

  1) ANILA_DEV_TLS_CERTS_DIR — repo 外絕對路徑（內需 server.crt + server.key）
     自簽一行式：
       CERT_DIR="$HOME/.local/state/anila/dev-tls" && mkdir -p "$CERT_DIR" && openssl req -x509 -nodes -days 3650 -newkey rsa:2048 -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" -subj "/C=TW/ST=Taiwan/L=Taipei/O=ANILA/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
       # .env：ANILA_DEV_TLS_CERTS_DIR=$HOME/.local/state/anila/dev-tls（寫成展開後的絕對路徑）

  2) EMBEDDING_MODEL_FINGERPRINT_DEV — sha256:<64 小寫 hex>（對應實際權重）
     純本機無真實權重可暫用全 0（僅限 dev）：
       EMBEDDING_MODEL_FINGERPRINT_DEV=sha256:0000000000000000000000000000000000000000000000000000000000000000

填完後：
  bash infra/deployment/scripts/dev-up.sh

EOF
  exit 1
fi

# ── 2. .env 健檢 ─────────────────────────────────────────────────────────
log "掃描 .env 占位符與必填項"
placeholder_keys=()
while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  [[ "$line" == *=* ]] || continue
  local_key="${line%%=*}"
  local_key="${local_key%"${local_key##*[![:space:]]}"}"
  local_key="${local_key#"${local_key%%[![:space:]]*}"}"
  local_val="${line#*=}"
  if [[ "$local_val" == *'<'*'>'* ]]; then
    placeholder_keys+=("$local_key")
  fi
done < "$ENV_FILE"

if ((${#placeholder_keys[@]} > 0)); then
  warn "下列變數仍為 <...> 占位符，請改成實際值："
  for k in "${placeholder_keys[@]}"; do
    printf '  - %s\n' "$k" >&2
  done
fi

tls_dir="$(env_file_value ANILA_DEV_TLS_CERTS_DIR)"
fingerprint="$(env_file_value EMBEDDING_MODEL_FINGERPRINT_DEV)"
missing_required=0

if [[ -z "$tls_dir" ]]; then
  missing_required=1
  err "缺少 ANILA_DEV_TLS_CERTS_DIR"
fi
if [[ -z "$fingerprint" ]]; then
  missing_required=1
  err "缺少 EMBEDDING_MODEL_FINGERPRINT_DEV"
fi

# 占位符也視為未就緒（與「缺失」同等阻擋）
if [[ "$tls_dir" == *'<'*'>'* ]]; then
  missing_required=1
  err "ANILA_DEV_TLS_CERTS_DIR 仍是占位符"
fi
if [[ "$fingerprint" == *'<'*'>'* ]]; then
  missing_required=1
  err "EMBEDDING_MODEL_FINGERPRINT_DEV 仍是占位符"
fi

if [[ "$missing_required" == "1" ]]; then
  cat <<'EOF' >&2

修復指令：

  # TLS 自簽一行式（repo 外目錄）
  CERT_DIR="$HOME/.local/state/anila/dev-tls" && mkdir -p "$CERT_DIR" && openssl req -x509 -nodes -days 3650 -newkey rsa:2048 -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" -subj "/C=TW/ST=Taiwan/L=Taipei/O=ANILA/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
  # 編輯 .env：ANILA_DEV_TLS_CERTS_DIR=<上列 CERT_DIR 的絕對路徑>

  # Embedding 指紋（對應實際權重；純本機可暫用全 0）
  # EMBEDDING_MODEL_FINGERPRINT_DEV=sha256:0000000000000000000000000000000000000000000000000000000000000000

EOF
  exit 1
fi

# ── 3. compose up ────────────────────────────────────────────────────────
csp_id_before="$(compose ps -aq csp 2>/dev/null || true)"
csp_created_before=""
if [[ -n "$csp_id_before" ]]; then
  csp_created_before="$(docker inspect -f '{{.Created}}' "$csp_id_before" 2>/dev/null || true)"
fi

log "docker compose -f $COMPOSE_FILE up -d --build"
compose up -d --build

csp_recreated=0
csp_id_after="$(compose ps -aq csp 2>/dev/null || true)"
csp_created_after=""
if [[ -n "$csp_id_after" ]]; then
  csp_created_after="$(docker inspect -f '{{.Created}}' "$csp_id_after" 2>/dev/null || true)"
fi
if [[ "$csp_id_before" != "$csp_id_after" || "$csp_created_before" != "$csp_created_after" ]]; then
  csp_recreated=1
  log "偵測到 csp 已 (re)create"
fi

# ── 4. Router 鏈祕密件 ───────────────────────────────────────────────────
registry_token="$(env_file_value ANILA_CSP_REGISTRY_SERVICE_TOKEN)"
if [[ -z "$registry_token" ]]; then
  log "ANILA_CSP_REGISTRY_SERVICE_TOKEN 為空 → 跑 router-chain-bootstrap.sh"
  # bootstrap 需要 csp 已起來且可 exec；先等 csp healthy 一小段再跑
  deadline=$(( $(date +%s) + CSP_WAIT_SECONDS ))
  while true; do
    status="$(compose ps csp --format '{{.Status}}' 2>/dev/null || true)"
    if [[ "$status" == *'(healthy)'* ]]; then
      break
    fi
    if [[ "$status" == *'(unhealthy)'* || "$status" == *'Restarting'* || "$status" == *'Exited'* ]]; then
      err "csp 未達 healthy（$status），無法跑 bootstrap"
      exit 1
    fi
    remaining=$(( deadline - $(date +%s) ))
    (( remaining > 0 )) || { err "等 csp healthy 逾時（${CSP_WAIT_SECONDS}s），無法跑 bootstrap"; exit 1; }
    sleep 3
  done
  bash "$BOOTSTRAP_SCRIPT"
  log "套用 bootstrap 寫入的 token：up -d --no-deps router csp"
  csp_id_pre_boot="$(compose ps -aq csp 2>/dev/null || true)"
  csp_created_pre_boot="$(docker inspect -f '{{.Created}}' "$csp_id_pre_boot" 2>/dev/null || true)"
  compose up -d --no-deps router csp
  csp_id_post_boot="$(compose ps -aq csp 2>/dev/null || true)"
  csp_created_post_boot="$(docker inspect -f '{{.Created}}' "$csp_id_post_boot" 2>/dev/null || true)"
  if [[ "$csp_id_pre_boot" != "$csp_id_post_boot" || "$csp_created_pre_boot" != "$csp_created_post_boot" ]]; then
    csp_recreated=1
    log "bootstrap 後 csp 已 (re)create"
  fi
else
  log "ANILA_CSP_REGISTRY_SERVICE_TOKEN 已設 → 跳過 bootstrap"
fi

# ── 5. 依賴重啟鐵則 ──────────────────────────────────────────────────────
if [[ "$csp_recreated" == "1" ]]; then
  log "csp 曾 (re)create → restart router anila-studio asr-gateway"
  compose restart router anila-studio asr-gateway
else
  log "csp 未 (re)create → 跳過依賴 restart"
fi

# ── 6. 收尾：等 csp healthy + 健康摘要 ───────────────────────────────────
log "等待 csp healthy（上限 ${CSP_WAIT_SECONDS}s）"
deadline=$(( $(date +%s) + CSP_WAIT_SECONDS ))
while true; do
  status="$(compose ps csp --format '{{.Status}}' 2>/dev/null || true)"
  if [[ "$status" == *'(healthy)'* ]]; then
    log "csp healthy"
    break
  fi
  if [[ "$status" == *'(unhealthy)'* ]]; then
    err "csp unhealthy：$status"
    compose ps
    exit 1
  fi
  remaining=$(( deadline - $(date +%s) ))
  (( remaining > 0 )) || {
    err "等 csp healthy 逾時（${CSP_WAIT_SECONDS}s）；目前：$status"
    compose ps
    exit 1
  }
  sleep 3
done

log "全服務健康摘要"
printf '%s\n' "------------------------------------------------------------"
compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Image}}'
printf '%s\n' "------------------------------------------------------------"

unhealthy=0
# 只檢查目前專案內有跑的服務；Status 含 (unhealthy) 即失敗
while IFS= read -r row; do
  [[ -n "$row" ]] || continue
  svc="${row%%$'\t'*}"
  st="${row#*$'\t'}"
  if [[ "$st" == *'(unhealthy)'* ]]; then
    err "$svc: $st"
    unhealthy=1
  fi
done < <(compose ps --format '{{.Service}}\t{{.Status}}' 2>/dev/null || true)

if [[ "$unhealthy" == "1" ]]; then
  err "有服務 unhealthy → exit 1"
  exit 1
fi

log "dev stack 就緒（project=anila-platform-dev）"
log "入口：https://localhost:8443 （UI https://localhost:9443）"
exit 0
