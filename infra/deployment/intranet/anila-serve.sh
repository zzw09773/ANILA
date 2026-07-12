#!/usr/bin/env bash
# anila-serve.sh — ANILA 平台服務管理 (薄包裝,實作在 deploy-prod.sh)
# ============================================================================
# 用法 (subcommand 與 deploy-prod.sh 完全相同):
#   bash infra/deployment/intranet/anila-serve.sh preflight       只跑 pre-flight 檢查
#   bash infra/deployment/intranet/anila-serve.sh deploy          preflight + build + up + verify
#   bash infra/deployment/intranet/anila-serve.sh up|down|restart
#   bash infra/deployment/intranet/anila-serve.sh rebuild <svc>   重建單一服務 (e.g. rebuild csp)
#   bash infra/deployment/intranet/anila-serve.sh status|logs <svc>
#   bash infra/deployment/intranet/anila-serve.sh codeserver-up|codeserver-down
#
# 跟 model-serve.sh 的分工:
#   anila-serve.sh  → 平台 stack (infra/compose/platform.yml, project: anila-platform)
#   model-serve.sh  → 模型 stack (infra/models/, project: anila-models)
# 兩個 compose project 生命週期獨立,互不誤殺。
#
# 自動 set -a source repo root .env — 跟 model-serve.sh 行為一致,
# 不用每次手動 source。
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"; set +a
fi

exec bash "$REPO_ROOT/infra/deployment/scripts/deploy-prod.sh" "$@"
