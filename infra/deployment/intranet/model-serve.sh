#!/usr/bin/env bash
# model-serve.sh — 模型推論服務管理 (infra/models/docker-compose.yml)
# ============================================================================
# 用法:
#   bash infra/deployment/intranet/model-serve.sh up <group|service...>   起服務
#   bash infra/deployment/intranet/model-serve.sh down [service...]       停 (不給名字 = 全停)
#   bash infra/deployment/intranet/model-serve.sh restart <service...>
#   bash infra/deployment/intranet/model-serve.sh status                  全部 health 一覽
#   bash infra/deployment/intranet/model-serve.sh logs <service>          tail -f
#
# Group (up 專用捷徑):
#   trial      試用機現役組: gpt-oss-20b gemma4 nv-embed (不含未核准 FLUX)
#   flux-approved  明示 legal-approved profile: flux2-dev + model-side shim
#   intranet   內網 H100 組:  gemma4(31B) 26b-a4b 12b 120b nv-embed
#
# 行為:
#   * 自動 set -a source repo root .env (INTERNAL_PLATFORM_API_KEY /
#     ANILA_HF_DIR / *_GPU 等都從那邊來)
#   * 透過 shared helper 建立/驗證 anila-models-net internal bridge
#   * 一律帶 --profile intranet,profile 內外的服務都可直接點名
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/models/docker-compose.yml"

# shellcheck disable=SC1091
source "$REPO_ROOT/infra/deployment/scripts/ensure-models-network.sh"

GROUP_TRIAL=(gpt-oss-20b gemma4 nv-embed-triton nv-embed-proxy)
GROUP_FLUX=(flux2-dev flux2-dev-agent)
GROUP_INTRANET=(gemma4 nv-embed-triton nv-embed-proxy gemma-4-26b-a4b gemma-4-12b gpt-oss-120b)

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"; set +a
fi

dc() { docker compose -f "$COMPOSE_FILE" --profile intranet "$@"; }

verify_flux_approval() {
  [[ "${GATE5_FLUX_LEGAL_APPROVED:-false}" == "true" || "${GATE5_FLUX_LEGAL_APPROVED:-0}" == "1" ]] \
    || { echo "FLUX 需要 GATE5_FLUX_LEGAL_APPROVED=true；預設保持停用" >&2; exit 1; }
  [[ -n "${FLUX_AGENT_SERVICE_TOKEN:-}" && "${FLUX_AGENT_SERVICE_TOKEN}" == csk-* ]] \
    || { echo "FLUX 需要專用 FLUX_AGENT_SERVICE_TOKEN (csk-...)" >&2; exit 1; }
  local material_dir="${GATE5_MATERIAL_DIR:-}"
  [[ -n "$material_dir" && "$material_dir" = /* ]] \
    || { echo "FLUX 需要 repo 外的 GATE5_MATERIAL_DIR" >&2; exit 1; }
  local file
  for file in inventory.json profile.json trust-store.json observed-facts.json; do
    [[ -f "$material_dir/$file" && ! -L "$material_dir/$file" ]] \
      || { echo "FLUX legal profile material 缺少 regular file: $material_dir/$file" >&2; exit 1; }
  done
  local verifier="$REPO_ROOT/infra/policy/gate5/check_model_governance.py"
  [[ -f "$verifier" ]] || { echo "找不到 Gate 5 governance verifier: $verifier" >&2; exit 1; }
  PYTHONPATH="$REPO_ROOT/packages/anila-security/src${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$verifier" \
      --inventory "$material_dir/inventory.json" \
      --profile "$material_dir/profile.json" \
      --trust-store "$material_dir/trust-store.json" \
      --repo-root "$REPO_ROOT" \
    || { echo "FLUX signed governance profile 驗證失敗" >&2; exit 1; }
  python3 - "$material_dir/profile.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    profile = json.load(handle)
enabled = profile.get("enabled_callsites", []) if isinstance(profile, dict) else []
if not any(isinstance(item, str) and "flux" in item.lower() for item in enabled):
    raise SystemExit("FLUX signed governance profile lacks a FLUX callsite binding")
PY
}

ensure_network() {
  ensure_models_network \
    || { echo "anila-models-net topology 驗證失敗；拒絕使用或修改既有 network" >&2; exit 1; }
}

verify_no_running_flux() {
  local service cid
  for service in anila-model-flux2-dev anila-model-flux2-dev-agent; do
    cid="$(docker ps -q --filter "name=^/${service}$" 2>/dev/null || true)"
    [[ -z "$cid" ]] || {
      echo "FLUX container $service 正在 running，但 GATE5_FLUX_LEGAL_APPROVED 未核准；拒絕繼續" >&2
      exit 1
    }
  done
}

verify_flux_compose_readback() {
  local resolved
  resolved="$(dc --profile flux-approved config --format json)" \
    || { echo "無法 read-back --profile flux-approved 的 model Compose" >&2; exit 1; }
  printf '%s' "$resolved" | python3 -c '
import json
import sys

doc = json.load(sys.stdin)
services = doc.get("services", {})
required = ("flux2-dev", "flux2-dev-agent")
missing = [name for name in required if name not in services]
if missing:
    raise SystemExit(f"flux-approved resolved Compose 缺少服務: {missing}")
for name in required:
    service = services[name]
    labels = service.get("labels", {})
    if labels.get("com.anila.required-profile") != "flux-approved":
        raise SystemExit(f"{name} 缺少 com.anila.required-profile=flux-approved")
    networks = service.get("networks", {})
    if "models" not in networks:
        raise SystemExit(f"{name} 未加入 models network")
    if service.get("ports"):
        raise SystemExit(f"{name} 不得暴露 host ports")
    expose = service.get("expose", [])
    if "8000" not in {str(item) for item in expose}:
        raise SystemExit(f"{name} 缺少 internal expose 8000")
backend = services["flux2-dev-agent"].get("environment", {}).get("FLUX_BACKEND_URL", "")
if not str(backend).startswith("http://flux2-dev:"):
    raise SystemExit("flux2-dev-agent FLUX_BACKEND_URL 必須指向 flux2-dev")
' || { echo "flux-approved resolved Compose read-back 不完整" >&2; exit 1; }
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  up)
    (( $# > 0 )) || { echo "up 要指定 group (trial|intranet) 或服務名,避免起錯組"; exit 1; }
    ensure_network
    services=()
    flux_profile=0
    for a in "$@"; do
      case "$a" in
        trial)    verify_no_running_flux; services+=("${GROUP_TRIAL[@]}") ;;
        flux-approved) verify_flux_approval; verify_flux_compose_readback; services+=("${GROUP_FLUX[@]}"); flux_profile=1 ;;
        intranet) services+=("${GROUP_INTRANET[@]}") ;;
        flux2-dev|flux2-dev-agent)
          echo "FLUX 服務需要明示 flux-approved profile 與法務核准證據" >&2
          exit 1
          ;;
        *)        services+=("$a") ;;
      esac
    done
    if (( flux_profile )); then
      dc --profile flux-approved up -d --no-build "${services[@]}"
      dc --profile flux-approved ps "${services[@]}"
    else
      dc up -d --no-build "${services[@]}"
      dc ps "${services[@]}"
    fi
    ;;
  down)
    if (( $# > 0 )); then dc stop "$@" && dc rm -f "$@"; else dc down; fi
    ;;
  restart)
    (( $# > 0 )) || { echo "restart 要指定服務名"; exit 1; }
    ensure_network
    for a in "$@"; do
      case "$a" in
        flux2-dev|flux2-dev-agent) verify_flux_approval; verify_flux_compose_readback ;;
        *) verify_no_running_flux ;;
      esac
    done
    dc restart "$@"
    ;;
  status)
    ensure_network
    dc ps
    ;;
  logs)
    (( $# > 0 )) || { echo "logs 要指定服務名"; exit 1; }
    dc logs -f --tail 100 "$@"
    ;;
  *)
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
esac
