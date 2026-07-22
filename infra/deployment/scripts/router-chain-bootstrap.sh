#!/usr/bin/env bash
# router-chain-bootstrap.sh — 一次性補齊「ANILA Router auto」聊天鏈的祕密件。
#
# 背景:Router sentinel 鏈的「非祕密件」(anila-router 模型列、trusted host
# `router`、sentinel endpoint)已由 CSP auto_seed + compose 預設自動補齊。祕密
# 件依安全紅線「祕密不得有預設值」不進 auto_seed,改由本腳本冪等寫入:
#   (i)  輪替/建立 service_clients 的 `router-primary` token(grace window);
#   (ii) 設定 anila-router 模型列的 gateway api_key(僅在操作者提供時);
#   (iii)把新 token 寫進 repo-root .env 的 ANILA_CSP_{REGISTRY,AGENT,INFERENCE}_
#        SERVICE_TOKEN(就地取代或新增)、chmod 600 .env。
# 祕密值「絕不」印到 stdout(只印變數名 + 已寫入 .env)。寫完只「提醒」操作者
# 重啟 router/csp,不代跑。
#
# 機制:透過 `docker compose exec -T csp python3` 直接操作 CSP 的 SQLAlchemy
# session(與 gate5-silver-e2e.sh / gate5-silver-seed.py 同一條既有路徑),不需
# 簽發 admin JWT。token 於 host 端鑄造後以 -e 注入容器,永不從容器 stdout 帶出。
#
# 用法:
#   infra/deployment/scripts/router-chain-bootstrap.sh [--dry-run]
# 環境變數(選填):
#   ROUTER_SENTINEL_GATEWAY_API_KEY  anila-router 模型列要寫入的 gateway api_key。
#                                    未設 = 跳過該步(可日後於模型管理介面補)。
#   ROUTER_CHAIN_COMPOSE_FILE        compose 檔(預設 compose.dev.yaml)。
#   ROUTER_CHAIN_GRACE_SECONDS       輪替 grace 秒數(預設 86400)。
set -euo pipefail
IFS=$'\n\t'

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="${ROUTER_CHAIN_COMPOSE_FILE:-compose.dev.yaml}"
GRACE_SECONDS="${ROUTER_CHAIN_GRACE_SECONDS:-86400}"
ENV_FILE="$ROOT_DIR/.env"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,26p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "ERROR: 未知參數 '$arg'(僅支援 --dry-run)" >&2
      exit 2
      ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: 需要 host 端 python3 以鑄造 token 與寫 .env" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: 需要 docker CLI" >&2
  exit 2
fi

GATEWAY_KEY="${ROUTER_SENTINEL_GATEWAY_API_KEY:-}"
TOKEN_ENV_KEYS=(
  ANILA_CSP_REGISTRY_SERVICE_TOKEN
  ANILA_CSP_AGENT_SERVICE_TOKEN
  ANILA_CSP_INFERENCE_SERVICE_TOKEN
)

echo "== Router chain bootstrap plan =="
echo "  compose 檔      : $COMPOSE_FILE"
echo "  .env           : $ENV_FILE"
echo "  service client : router-primary(rotate/create, grace=${GRACE_SECONDS}s)"
if [[ -n "$GATEWAY_KEY" ]]; then
  echo "  gateway api_key: anila-router 模型列將寫入(值來自 ROUTER_SENTINEL_GATEWAY_API_KEY)"
else
  echo "  gateway api_key: 跳過(未設 ROUTER_SENTINEL_GATEWAY_API_KEY)"
fi
(IFS=', '; echo "  .env 寫入變數   : ${TOKEN_ENV_KEYS[*]}")

if [[ "$DRY_RUN" == "1" ]]; then
  echo "-- --dry-run:不連 csp、不動 DB、不寫 .env,結束(exit 0)。"
  exit 0
fi

# 真跑:csp 容器/DB 不可達即大聲失敗(set -e 捕捉非零退出)。
if ! docker compose -f "$COMPOSE_FILE" exec -T csp "$PYTHON_BIN" -c "import app.database" \
  >/dev/null 2>&1; then
  echo "ERROR: csp 容器不可達(docker compose -f $COMPOSE_FILE exec csp 失敗)。" >&2
  echo "       請先啟動 stack(docker compose -f $COMPOSE_FILE up -d csp)後再跑。" >&2
  exit 1
fi

# host 端鑄造 token(csk- + token_urlsafe(32),與 generate_service_token 同格式)。
# 只在 host 記憶體與 .env 存在,經 -e 注入容器,永不從容器 stdout 帶出。
ROUTER_TOKEN="csk-$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')"

export BOOTSTRAP_ROUTER_TOKEN="$ROUTER_TOKEN"
export BOOTSTRAP_GATEWAY_KEY="$GATEWAY_KEY"
export BOOTSTRAP_GRACE_SECONDS="$GRACE_SECONDS"

# 於 csp 容器內操作 SQLAlchemy session。只印「非祕密」狀態行到 stdout。
if ! docker compose -f "$COMPOSE_FILE" exec -T \
  -e BOOTSTRAP_ROUTER_TOKEN \
  -e BOOTSTRAP_GATEWAY_KEY \
  -e BOOTSTRAP_GRACE_SECONDS \
  csp "$PYTHON_BIN" - <<'PY'
import os
import sys
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.service_client import ServiceClient
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
)

token = os.environ["BOOTSTRAP_ROUTER_TOKEN"]
gateway = os.environ.get("BOOTSTRAP_GATEWAY_KEY", "").strip()
grace = int(os.environ.get("BOOTSTRAP_GRACE_SECONDS", "86400"))

db = SessionLocal()
try:
    now = datetime.now(timezone.utc)
    client = (
        db.query(ServiceClient)
        .filter(ServiceClient.client_name == "router-primary")
        .first()
    )
    if client is None:
        client = ServiceClient(
            client_name="router-primary",
            client_type="router",
            description="Router primary s2s token (router-chain-bootstrap)",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
            is_legacy=False,
            is_active=True,
        )
        db.add(client)
        action = "created"
    else:
        if not client.is_active:
            print("ERROR: router-primary 已撤銷,拒絕輪替", file=sys.stderr)
            sys.exit(1)
        # grace rotation:舊 token 於 grace 內仍可驗(避免 restart 空窗)。
        client.service_token_previous_envelope = client.service_token_envelope
        client.service_token_previous_lookup_hash = client.service_token_lookup_hash
        client.service_token_previous_expires_at = now + timedelta(seconds=grace)
        client.service_token_envelope = encode_service_token_envelope(token)
        client.service_token_lookup_hash = compute_lookup_hash(token)
        client.service_token_rotated_at = now
        client.is_legacy = False
        action = "rotated"
    db.flush()

    model_status = "skipped (no gateway key supplied)"
    if gateway:
        model = (
            db.query(ModelRegistry)
            .filter(ModelRegistry.name == "anila-router")
            .first()
        )
        if model is None:
            print(
                "ERROR: anila-router 模型列不存在。請先啟動 csp 讓 auto_seed 建列"
                "(需設 ANILA_ROUTER_SENTINEL_URL),再重跑本腳本。",
                file=sys.stderr,
            )
            sys.exit(1)
        model.api_key_secret_ref = encode_service_token_envelope(gateway)
        model_status = "set"

    db.commit()
    # 只印非祕密狀態。
    print(f"OK  service_client router-primary {action}")
    print(f"OK  anila-router gateway api_key {model_status}")
finally:
    db.close()
PY
then
  echo "ERROR: csp 內 bootstrap 失敗(見上方 stderr)。.env 未變更。" >&2
  exit 1
fi

# 就地 upsert .env(host 端 python,避免 sed 轉義陷阱)。祕密值只寫檔、不印。
ROUTER_TOKEN="$ROUTER_TOKEN" ENV_FILE="$ENV_FILE" \
  TOKEN_ENV_KEYS="${TOKEN_ENV_KEYS[*]}" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

env_file = Path(os.environ["ENV_FILE"])
token = os.environ["ROUTER_TOKEN"]
keys = os.environ["TOKEN_ENV_KEYS"].split()

lines = env_file.read_text().splitlines() if env_file.exists() else []
by_key = {k: f"{k}={token}" for k in keys}

out = []
seen = set()
for line in lines:
    stripped = line.lstrip()
    matched = None
    if "=" in stripped and not stripped.startswith("#"):
        name = stripped.split("=", 1)[0].strip()
        if name in by_key:
            matched = name
    if matched is not None:
        out.append(by_key[matched])
        seen.add(matched)
    else:
        out.append(line)

for k in keys:
    if k not in seen:
        out.append(by_key[k])

env_file.write_text("\n".join(out) + "\n")
os.chmod(env_file, 0o600)
PY

(IFS=', '; echo "OK  已寫入 .env(chmod 600):${TOKEN_ENV_KEYS[*]}(值不印出)")
echo
echo "下一步(請自行執行,本腳本不代跑):"
echo "  docker compose -f $COMPOSE_FILE up -d --no-deps router csp"
