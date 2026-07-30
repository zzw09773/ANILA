#!/usr/bin/env bash
# 繁體中文(台灣用語)語言政策 — 手動檢查（非 CI gate）。
# 掃描前端使用者字串 + 後端使用者可見錯誤字串是否含簡體字 / 大陸用語。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${ZHTW_PY:-python3}"

# 三前端 + CSP API + Studio（使用者可見錯誤／文案）
"$PY" "$ROOT/infra/ci/lint_zh_tw.py" "$ROOT/apps"
"$PY" "$ROOT/infra/ci/lint_zh_tw.py" "$ROOT/services/csp/app"
"$PY" "$ROOT/infra/ci/lint_zh_tw.py" "$ROOT/services/anila-studio/app"
