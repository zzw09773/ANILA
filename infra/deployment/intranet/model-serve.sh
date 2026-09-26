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
#   trial      試用機現役組: gpt-oss-20b gemma4 nv-embed
#   intranet   內網 H100 組:  gemma4(31B) 26b-a4b 12b 120b nv-embed
#
# 行為:
#   * 自動 set -a source repo root .env (INTERNAL_PLATFORM_API_KEY /
#     ANILA_HF_DIR / *_GPU 等都從那邊來)
#   * 自動建 anila-models-net external network (不存在時)
#   * 一律帶 --profile intranet,profile 內外的服務都可直接點名
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/infra/models/docker-compose.yml"

GROUP_TRIAL=(gpt-oss-20b gemma4 nv-embed-triton nv-embed-proxy)
GROUP_INTRANET=(gemma4 nv-embed-triton nv-embed-proxy gemma-4-26b-a4b gemma-4-12b gpt-oss-120b)

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"; set +a
fi

dc() { docker compose -f "$COMPOSE_FILE" --profile intranet "$@"; }

ensure_network() {
  docker network inspect anila-models-net >/dev/null 2>&1 \
    || { echo "建立 anila-models-net network"; docker network create anila-models-net >/dev/null; }
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  up)
    (( $# > 0 )) || { echo "up 要指定 group (trial|intranet) 或服務名,避免起錯組"; exit 1; }
    ensure_network
    services=()
    for a in "$@"; do
      case "$a" in
        trial)    services+=("${GROUP_TRIAL[@]}") ;;
        intranet) services+=("${GROUP_INTRANET[@]}") ;;
        *)        services+=("$a") ;;
      esac
    done
    dc up -d --no-build "${services[@]}"
    dc ps "${services[@]}"
    ;;
  down)
    if (( $# > 0 )); then dc stop "$@" && dc rm -f "$@"; else dc down; fi
    ;;
  restart)
    (( $# > 0 )) || { echo "restart 要指定服務名"; exit 1; }
    dc restart "$@"
    ;;
  status)
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
