#!/usr/bin/env bash
# 在一次性容器裡跑後端測試 —— 開發輔助工具(不是 CI 的一部分)。
#
# 為什麼需要這支
# --------------
# 1. **Python 版本**:生產容器是 3.11,而本機系統 python 是 3.10。3.10 下
#    `asyncio.TimeoutError is not TimeoutError`,會讓 `test_proxy_stream_usage`
#    這類測試出現**假失敗**;repo 內也沒有 venv,而 `services/csp` 沒宣告
#    `requires-python`,所以沒有東西會提醒你版本不對。
# 2. **不動 user 的 dev stack**:`anila-platform-*` 是 user 的開發環境,鐵則
#    禁止動它。這裡用 `--rm` 開一次性容器、repo 唯讀掛載,零影響。
# 3. **三個必須設對的環境細節**(每一個都踩過):
#    - `logs/` 要可寫:app 的 logging 寫 `services/csp/logs/csp.log`,唯讀掛載下
#      整批測試會以 `PermissionError` 收場(實測 464 個 error 全是這個)。
#    - `PYTHONPATH` 必須把 `packages/*/src` 放在前面:容器 image 裡**已安裝**
#      舊版 anila-core / anila-contracts,不設路徑的話你改的源碼會被 site-packages
#      遮蔽,測到的是舊碼(症狀:`unexpected keyword argument` 之類)。
#    - `-p no:cacheprovider`:唯讀掛載下 pytest 寫不了 .pytest_cache。
#
# 用法:
#   infra/ci/run-backend-tests.sh                          # CSP 全套
#   infra/ci/run-backend-tests.sh tests/test_foo.py -x     # 指定目標
#   TARGET_DIR=packages/anila-core infra/ci/run-backend-tests.sh tests/
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${ANILA_TEST_IMAGE:-anila-platform-dev-csp}"
TARGET_DIR="${TARGET_DIR:-services/csp}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "找不到 image '$IMAGE'。設 ANILA_TEST_IMAGE 或先建置 dev stack。" >&2
  exit 2
fi

# 源碼路徑排在最前面,壓過 image 內已安裝的舊版套件
PYPATH="/work/${TARGET_DIR}"
for pkg in anila-core anila-contracts anila-security anila-agent; do
  [ -d "$REPO_ROOT/packages/$pkg/src" ] && PYPATH="$PYPATH:/work/packages/$pkg/src"
done
[ -d "$REPO_ROOT/services/ingestion-worker/src" ] && \
  PYPATH="$PYPATH:/work/services/ingestion-worker/src"

# logs/ 的 tmpfs 只在該目錄**已存在於 repo** 時才掛 —— 唯讀掛載下 docker 無法
# 建立不存在的掛載點(會以 "read-only file system" 失敗)。目前只有
# services/csp 有 logs/,它也是唯一會在 import 時開 file handler 的服務。
MOUNTS=(--tmpfs /tmp:mode=1777)
if [ -d "$REPO_ROOT/${TARGET_DIR}/logs" ]; then
  MOUNTS+=(--tmpfs "/work/${TARGET_DIR}/logs:mode=1777")
fi

exec docker run --rm \
  -v "$REPO_ROOT:/work:ro" \
  "${MOUNTS[@]}" \
  -w "/work/${TARGET_DIR}" \
  -e "PYTHONPATH=$PYPATH" \
  -e SECRET_KEY=test-only-key-0123456789abcdef \
  "$IMAGE" \
  python -m pytest -p no:cacheprovider --no-header "$@"
