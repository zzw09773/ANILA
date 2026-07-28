#!/usr/bin/env bash
# 把側錄資料變成一份可以拿去要資源的證據摘要。
#
# 這支存在的理由:一份「它掛了」的報告換不到任何東西,一份「在 N 個併發時
# 上游模型端點開始回 429/5xx、而平台自身的 CPU 與連線池都還沒滿」的報告
# 才指得出瓶頸在哪個資源。兩者的差別只在有沒有人把數字整理出來。
#
#   ./infra/capture/summarize-incident.sh <標籤>
#
# 讀 infra/capture/results/<標籤>/,輸出 summary.md 到同一個目錄。
set -uo pipefail

LABEL="${1:?用法: summarize-incident.sh <標籤>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIR="$REPO_ROOT/infra/capture/results/$LABEL"
[ -d "$DIR" ] || { echo "找不到 $DIR" >&2; exit 1; }
OUT="$DIR/summary.md"

{
  echo "# 開放窗口現場摘要 —— $LABEL"
  echo
  echo '> 由 `infra/capture/summarize-incident.sh` 產生。原始取樣同目錄。'
  echo '> **判讀原則**:先看「瓶頸歸屬」那一節。平台自身資源沒滿、而上游'
  echo '> 模型端點在同一時刻開始拒絕,才構成「算力不足」的論據;若平台自己'
  echo '> 的連線池或 CPU 先滿,那是平台要改,不是要更多算力。'
  echo
  echo '## 環境'
  echo '```'
  cat "$DIR/meta.txt" 2>/dev/null
  echo '```'
  echo

  # ── 請求速率、錯誤率、延遲 ────────────────────────────────────────────
  echo '## 請求量與失效'
  if [ -s "$DIR/access.log" ]; then
    echo
    echo '| 指標 | 值 |'
    echo '|---|---|'
    total=$(grep -c '→' "$DIR/access.log" 2>/dev/null || echo 0)
    echo "| 總請求數 | $total |"
    for code in 200 401 403 429 500 502 503 504; do
      n=$(grep -c "→ $code " "$DIR/access.log" 2>/dev/null || echo 0)
      [ "$n" -gt 0 ] && echo "| HTTP $code | $n |"
    done
    # 429 是 nginx 限流(本次刻意改成 429 以便和上游 503 區分)
    n429=$(grep -c '→ 429 ' "$DIR/access.log" 2>/dev/null || echo 0)
    if [ "$n429" -gt 0 ]; then
      echo
      echo "⚠ **有 $n429 筆 429** —— 那是 nginx 的**限流**把人擋掉,不是算力不足。"
      echo "這些請求從未到達模型。要主張算力不足,得先把限流排除。"
    fi
    echo
    echo '延遲分布(毫秒,取自存取日誌):'
    echo '```'
    grep -oP '→ \d+ \K\d+(?=ms)' "$DIR/access.log" 2>/dev/null | sort -n | awk '
      {v[NR]=$1}
      END{
        if(NR==0){print "（無資料）"; exit}
        printf "n=%d  p50=%d  p90=%d  p95=%d  p99=%d  max=%d\n",
          NR, v[int(NR*0.50)+0], v[int(NR*0.90)+0], v[int(NR*0.95)+0],
          v[int(NR*0.99)+0], v[NR]
      }'
    echo '```'
  else
    echo '（`access.log` 是空的 —— 容器可能已重啟,日誌被丟掉了。）'
  fi
  echo

  # ── 平台自身資源 ──────────────────────────────────────────────────────
  echo '## 平台自身有沒有先撞牆'
  echo
  echo '這一節決定論述成不成立。若下列任一項在失效當下已經見底,那就是**平台**'
  echo '的限制先咬,不能拿來主張算力不足。'
  echo
  if [ -s "$DIR/pg.tsv" ]; then
    echo '### PostgreSQL 連線'
    echo '```'
    awk -F'\t' 'NR>1{
      if($2>mx)mx=$2; if($5>mit)mit=$5; if($6>ma)ma=$6;
      if($7>mb)mb=$7; if($8>mw)mw=$8; n++
    } END{
      if(n==0){print "（無資料）"; exit}
      printf "取樣數           : %d\n", n
      printf "連線數峰值       : %d\n", mx
      printf "idle-in-tx 峰值  : %d\n", mit
      printf "最長交易(秒)     : %.1f\n", ma
      printf "曾被阻塞的 session: %d\n", mb
      printf "等鎖的 session   : %d\n", mw
    }' "$DIR/pg.tsv"
    echo '```'
    echo
    echo '對照:應用池上限 = `ANILA_DB_POOL_SIZE + ANILA_DB_MAX_OVERFLOW`,'
    echo 'PG 上限 = 上方 meta 的 `max_connections`。連線數峰值若貼齊應用池上限,'
    echo '**瓶頸是池不是算力**。'
    echo
  fi
  if [ -s "$DIR/host.tsv" ]; then
    echo '### 主機'
    echo '```'
    awk -F'\t' 'NR>1{
      if($2>l1)l1=$2; if($4<mm||mm==0)mm=$4; if($5<dd||dd==0)dd=$5; if($6>dp)dp=$6; n++
    } END{
      if(n==0){print "（無資料）"; exit}
      printf "load average 峰值 : %.2f\n", l1
      printf "可用記憶體最低    : %d MB\n", mm
      printf "可用磁碟最低      : %d GB   (已用峰值 %d%%)\n", dd, dp
    }' "$DIR/host.tsv"
    echo '```'
    echo
    echo '⚠ load average 要跟 meta 裡的處理器數比。峰值遠低於核心數 = CPU 沒滿,'
    echo '瓶頸在別處(通常是等上游 I/O)。'
    echo
  fi
  if [ -s "$DIR/containers.tsv" ]; then
    echo '### 各容器 CPU 峰值'
    echo '```'
    awk -F'\t' 'NR>1{gsub(/%/,"",$3); if($3+0>m[$2])m[$2]=$3+0}
      END{for(k in m) printf "%-42s %6.1f%%\n", k, m[k]}' "$DIR/containers.tsv" | sort -k2 -rn | head -15
    echo '```'
    echo
  fi

  # ── 上游 ─────────────────────────────────────────────────────────────
  echo '## 上游模型端點'
  if [ -s "$DIR/gateway.tsv" ] && [ "$(wc -l < "$DIR/gateway.tsv")" -gt 1 ]; then
    echo '```'
    awk -F'\t' 'NR>1 && $2!=""{
      n++; c[$2]++; if($3+0>mx)mx=$3+0; s+=$3
    } END{
      if(n==0){print "（未設定 gateway 探針）"; exit}
      printf "探針次數     : %d\n", n
      printf "平均延遲     : %.2f s\n", s/n
      printf "最長延遲     : %.2f s\n", mx
      print  "狀態碼分布   :"
      for(k in c) printf "   %-6s %d 次 (%.1f%%)\n", k, c[k], 100*c[k]/n
    }' "$DIR/gateway.tsv"
    echo '```'
    echo
    echo '**這是論據的核心。** 探針是單發極小請求 —— 它變慢或開始非 200,'
    echo '代表上游在同時服務的負載下已經吃不下,而那與平台無關。'
  else
    echo '（未設定 `ANILA_GATEWAY_URL`,沒有上游探針。**強烈建議下次要設**:'
    echo '沒有這一欄,就無法把「平台撐不住」與「模型端點吃不下」分開,'
    echo '而那正是要資源時唯一會被追問的問題。）'
  fi
  echo

  echo '## 瓶頸歸屬(自行核對後填寫)'
  echo
  echo '| 判準 | 資料在哪 | 成立? |'
  echo '|---|---|---|'
  echo '| 上游探針在尖峰時延遲暴增或非 200 | `gateway.tsv` | |'
  echo '| PG 連線數**未**貼齊應用池上限 | `pg.tsv` vs meta | |'
  echo '| 主機 load average 遠低於核心數 | `host.tsv` vs meta | |'
  echo '| 存取日誌**沒有**大量 429(非限流所致) | 上方請求量表 | |'
  echo '| 磁碟未接近滿 | `host.tsv` | |'
  echo
  echo '五項全部成立 → 瓶頸在模型服務層,要的是**更多推論算力**。'
  echo '任一項不成立 → 先修那一項,否則論據會被該項駁回。'
} > "$OUT"

echo "摘要 → $OUT" >&2
command -v head >/dev/null && sed -n '1,20p' "$OUT"
