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
# 已知的環境差異(**不是** regression,別去「修」它):
#   - `tests/test_agent_registry_upgrade.py::...test_trace_emitter_posts_to_csp_asgi...`
#     會以 `ModuleNotFoundError: No module named 'agents'` 失敗 —— image 內沒裝
#     OpenAI Agents SDK(CI 的 backend-agent job 才裝)。所以本腳本的預期結果是
#     **1 failed / 1837 passed**,而那 1 個就是它。
#   - `infra/deployment/tests` 有一批測試會往 repo root 寫暫存檔,唯讀掛載下會噴
#     69 個 OSError。要跑那批請改用可寫副本:
#       docker run --rm -v "$PWD:/src:ro" --tmpfs /work:mode=1777,size=1g --user root \
#         -e PYTHONPATH=/work anila-platform-dev-csp \
#         sh -c 'cp -r /src/. /work/ && cd /work && python -m unittest discover -s infra/deployment/tests'
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

# ── 為什麼不是「repo 唯讀掛載 + 對 logs/ 掛 tmpfs」 ─────────────────────────
#
# 第一版是那樣寫的,而且只在 `${TARGET_DIR}/logs` **已存在**時才掛 tmpfs
# (唯讀掛載下 docker 無法建立不存在的掛載點)。問題是 `services/csp/logs/` 被
# .gitignore 忽略 → **乾淨 checkout 上它不存在** → 條件式跳過 tmpfs → CSP import
# 時的 `setup_logging()` 執行 `Path("logs").mkdir()` 撞唯讀檔案系統 → 一支測試
# 都還沒跑就整批失敗。也就是說那個版本**只在「host 上剛好殘留過那個 gitignored
# 目錄」時才會work**,對別人的機器或 CI runner 都是壞的。
# 由 PR #52 的 Codex review 抓到。
#
# 改法:把 repo 掛成 `/src:ro`,在容器內的 tmpfs `/work` 做一份可寫副本再跑。
# 代價是每次多一次 cp(repo 不大,實測 ~2 秒),換到的是「不依賴 host 上任何
# 偶然狀態」——而測試環境的可重現性比那兩秒值錢。副本在 tmpfs 上,容器結束即消失,
# 也不會有測試把暫存檔寫回你的工作樹的風險(`infra/deployment/tests` 就會那樣做)。
exec docker run --rm \
  -v "$REPO_ROOT:/src:ro" \
  --tmpfs /work:mode=1777,size=2g \
  --tmpfs /tmp:mode=1777 \
  --user root \
  -e "PYTHONPATH=$PYPATH" \
  -e SECRET_KEY=test-only-key-0123456789abcdef \
  "$IMAGE" \
  sh -c 'cp -a /src/. /work/ 2>/dev/null || cp -r /src/. /work/;
         cd "/work/'"${TARGET_DIR}"'" &&
         mkdir -p logs &&
         exec python -m pytest -p no:cacheprovider --no-header '"$(printf '%q ' "$@")"
