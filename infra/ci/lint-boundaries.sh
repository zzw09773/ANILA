#!/usr/bin/env bash
# lint-boundaries — CSP module boundary gate(doc 10 §14 CI Gates)。
# 以 import-linter 檢查 services/csp/.importlinter 的邊界契約:
#   1. app.modules.{tasks,policy,launch} 互不 import(independence)
#   2. app.modules 不得 import app.api(api → modules 單向分層)
#
# 用法:bash infra/ci/lint-boundaries.sh
# 前置:services/csp/.venv 已安裝 requirements-dev.txt(含 import-linter)。
#
# 注意:必須用 lint-imports console script;
# `python -m importlinter.cli` 沒有 __main__ 入口,會靜默 exit 0(假陰性)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSP_DIR="${SCRIPT_DIR}/../../services/csp"
LINTER="${CSP_DIR}/.venv/bin/lint-imports"

if [[ ! -x "${LINTER}" ]]; then
    echo "lint-boundaries: 找不到 ${LINTER};請先在 services/csp/.venv 安裝 requirements-dev.txt(含 import-linter)" >&2
    exit 1
fi

cd "${CSP_DIR}"
exec "${LINTER}" --config .importlinter
