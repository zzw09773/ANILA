#!/usr/bin/env bash
# 刻意重複的檔案必須保持一致 —— 補救計畫 W3-12i。
#
# 為什麼需要這支
# --------------
# `apps/anilalm/src/asr/asrStream.js` 與 `apps/anila-shell/src/asr/asrStream.js`
# 是 **427 行 byte-identical** 的兩份。它們重複的原因是這個 repo 沒有 JS
# workspace(三個前端各自 `npm install`),所以沒有地方放共用套件。
#
# 問題不是「有重複」,是**沒有任何東西防它們悄悄分岔**。這種檔案的分岔特別難發現:
# 兩邊都還能跑,只是行為開始不一樣 —— 而 ASR 是即時串流,不一致的行為會被當成
# 「今天網路比較差」。
#
# 中期解是 workspace 化(掛 C4 決策文件的附帶議題)。短期用這支擋住分岔:diff
# 不為零就 fail,而且訊息要指出**該改兩邊**,不是叫人跑格式化工具。
#
# 用法:
#   bash infra/ci/check-duplicated-sources.sh
#
# 新增一組刻意重複的檔案時,加進 PAIRS 並在註解寫明「為什麼重複」——
# 沒有理由的重複應該被消掉,不是被登記。
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# 每組一行:`<檔A>|<檔B>|<為什麼重複>`
PAIRS=(
  "apps/anilalm/src/asr/asrStream.js|apps/anila-shell/src/asr/asrStream.js|無 JS workspace 可放共用套件;workspace 化掛 C4 附帶議題"
)

fail=0
for entry in "${PAIRS[@]}"; do
  IFS='|' read -r a b why <<<"$entry"
  if [ ! -f "$a" ] || [ ! -f "$b" ]; then
    echo "BROKEN: 登記的重複組有檔案不存在 —— $a / $b" >&2
    echo "        檔案被移動或改名時要同步更新 PAIRS(或者重複已經被消掉了,那就刪掉這組)。" >&2
    exit 1
  fi
  if diff -q "$a" "$b" >/dev/null; then
    echo "  一致  $a"
    echo "        $b"
  else
    fail=1
    echo "::error::這兩份檔案應該逐字相同,但已經分岔:" >&2
    echo "  $a" >&2
    echo "  $b" >&2
    echo "  重複的原因:$why" >&2
    echo "" >&2
    echo "  **改動要同時套到兩邊。** 只改一邊會讓兩個前端的行為開始不一致 ——" >&2
    echo "  而這種不一致特別難發現:兩邊都還能跑,只是行為不同。" >&2
    echo "" >&2
    diff -u "$a" "$b" | head -40 >&2
  fi
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "刻意重複的檔案皆一致(${#PAIRS[@]} 組)。"
