#!/usr/bin/env bash
# 繁體中文(台灣用語)語言政策 CI gate — doc 11。
# 掃描前端使用者字串 + 後端 detail= 是否含簡體字 / 大陸用語。
# 與 infra/ci/lint-boundaries.sh 並列;CI 兩者皆須通過。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${ZHTW_PY:-python3}"

# 掃描各前端 + CSP 後端 detail;examples/ docs/ tests/ 由腳本內建豁免。
"$PY" "$ROOT/infra/ci/lint_zh_tw.py" "$ROOT/apps"
"$PY" "$ROOT/infra/ci/lint_zh_tw.py" "$ROOT/services/csp/app"
