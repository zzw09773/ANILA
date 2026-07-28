#!/usr/bin/env bash
# 現場側錄 —— 在開放窗口期間持續取樣，把「平台為什麼撐不住」變成可辯護的數據。
#
# 為什麼需要這支：平台**沒有 `/metrics`**，沒有延遲或錯誤率指標。`/api/usage`
# 是 token 用量的商業分析（誰用了幾個 token），回答不了「請求在哪裡塞住」。
# 唯一能重建現場的是 csp 的 access log 加上 PostgreSQL 的即時狀態 —— 而兩者
# 都是**不抓就沒有**的東西：容器重啟、日誌輪替、負載結束之後 pg_stat_activity
# 就歸零，事後誰都補不回來。
#
# 一次沒被量到的當機，換不到任何資源；它只會被讀成「系統做得不好」。這支的
# 目的就是讓失效帶著數字。
#
#   ./infra/capture/capture-incident.sh <標籤> [取樣間隔秒] [總時長秒]
#
# 例：整個上午的開放窗口，每 15 秒一筆
#   ./infra/capture/capture-incident.sh launch-am 15 14400
#
# 產出目錄 infra/capture/results/<標籤>/：
#   pg.tsv          連線數、idle-in-transaction、最長交易、阻塞鏈
#   containers.tsv  每個容器的 CPU / 記憶體
#   host.tsv        load average、可用記憶體、磁碟
#   gateway.tsv     上游模型端點的單發探針（延遲與狀態碼）
#   access.log      csp 存取日誌快照（含狀態碼與毫秒數）
#   meta.txt        機器規格與設定姿態，事後判讀用
#
# 安全：全程唯讀。不重啟任何容器、不改設定、不碰資料。
set -uo pipefail   # 刻意不用 -e：單一探針失敗不該中斷整場側錄

LABEL="${1:?用法: capture-incident.sh <標籤> [間隔秒] [總時長秒]}"
INTERVAL="${2:-15}"
TOTAL="${3:-14400}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO_ROOT/infra/capture/results/$LABEL"
mkdir -p "$OUT"

# 容器名稱可覆寫：正式 stack 的 project 名是 anila-platform，dev 是
# anila-platform-dev，兩者的容器前綴不同。
PROJECT="${ANILA_PROJECT:-anila-platform}"
DB_CONTAINER="${ANILA_DB_CONTAINER:-${PROJECT}-csp-db-1}"
CSP_CONTAINER="${ANILA_CSP_CONTAINER:-${PROJECT}-csp-1}"
DB_USER="${ANILA_DB_USER:-csp}"
DB_NAME="${ANILA_DB_NAME:-csp}"

# 上游模型探針（選用）。沒設就跳過該欄，其餘照抓。
GW_URL="${ANILA_GATEWAY_URL:-}"
GW_KEY="${ANILA_GATEWAY_KEY:-}"
GW_MODEL="${ANILA_CHAT_MODEL:-}"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ── meta：事後判讀時，機器規格與設定姿態跟數字一樣重要 ──────────────────
{
  echo "標籤        : $LABEL"
  echo "開始(UTC)   : $(ts)"
  echo "取樣間隔    : ${INTERVAL}s   總時長: ${TOTAL}s"
  echo "compose專案 : $PROJECT"
  echo
  echo "── 主機 ──"
  echo "CPU         : $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs)"
  echo "核心/執行緒 : $(nproc) 個可用處理器"
  echo "記憶體      : $(free -h | awk '/^Mem:/{print $2}')"
  echo "核心版本    : $(uname -r)"
  echo
  echo "── 關鍵設定姿態（決定失效長什麼樣）──"
  for v in LLM_TIMEOUT ANILA_DB_POOL_SIZE ANILA_DB_MAX_OVERFLOW \
           ANILA_DB_POOL_TIMEOUT_S ANILA_DB_IDLE_TX_TIMEOUT_MS \
           ENABLE_MEMORY ENABLE_PUBLIC_SHARE; do
    val="$(docker exec "$CSP_CONTAINER" printenv "$v" 2>/dev/null || echo '(未注入容器)')"
    printf '%-28s %s\n' "$v" "$val"
  done
  echo
  echo "PG max_connections : $(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAc 'SHOW max_connections' 2>/dev/null || echo '(取不到)')"
} > "$OUT/meta.txt" 2>&1

# ── 表頭 ───────────────────────────────────────────────────────────────────
[ -s "$OUT/pg.tsv" ] || printf 'ts\ttotal\tactive\tidle\tidle_in_tx\tmax_tx_age_s\tblocked\twaiting_locks\n' > "$OUT/pg.tsv"
[ -s "$OUT/host.tsv" ] || printf 'ts\tload1\tload5\tmem_avail_mb\tdisk_avail_gb\tdisk_used_pct\n' > "$OUT/host.tsv"
[ -s "$OUT/containers.tsv" ] || printf 'ts\tname\tcpu_pct\tmem_usage\tmem_pct\n' > "$OUT/containers.tsv"
[ -s "$OUT/gateway.tsv" ] || printf 'ts\thttp_code\tlatency_s\n' > "$OUT/gateway.tsv"

sample_pg() {
  # 一次查詢取回全部計數，避免多次往返在高負載下自己變成噪音。
  docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAF$'\t' -c "
    SELECT
      count(*),
      count(*) FILTER (WHERE state = 'active'),
      count(*) FILTER (WHERE state = 'idle'),
      count(*) FILTER (WHERE state = 'idle in transaction'),
      COALESCE(ROUND(MAX(EXTRACT(EPOCH FROM (now() - xact_start)))::numeric, 1), 0),
      count(*) FILTER (WHERE cardinality(pg_blocking_pids(pid)) > 0),
      count(*) FILTER (WHERE wait_event_type = 'Lock')
    FROM pg_stat_activity
    WHERE datname = '$DB_NAME';
  " 2>/dev/null | head -1
}

sample_gateway() {
  [ -z "$GW_URL" ] && { echo -e "\t"; return; }
  # 極小的一發請求：只問「上游還收不收、要多久」，不是壓測。
  curl -sS -o /dev/null -m 30 \
    -w '%{http_code}\t%{time_total}' \
    -H "Authorization: Bearer $GW_KEY" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$GW_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":1}" \
    "${GW_URL%/}/chat/completions" 2>/dev/null || echo -e "000\t0"
}

echo "側錄開始 → $OUT   （Ctrl-C 可隨時停止，已寫入的資料都保留）" >&2
END=$(( $(date +%s) + TOTAL ))
while [ "$(date +%s)" -lt "$END" ]; do
  NOW="$(ts)"

  PG="$(sample_pg)"
  [ -n "$PG" ] && printf '%s\t%s\n' "$NOW" "$PG" >> "$OUT/pg.tsv"

  read -r L1 L5 _ < /proc/loadavg
  MEM_AVAIL="$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)"
  read -r DISK_AVAIL DISK_PCT <<<"$(df -BG --output=avail,pcent / | tail -1 | tr -d 'G%')"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$NOW" "$L1" "$L5" "$MEM_AVAIL" "$DISK_AVAIL" "$DISK_PCT" >> "$OUT/host.tsv"

  docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null \
    | grep -E "^${PROJECT}|^anila-" \
    | while IFS= read -r line; do printf '%s\t%s\n' "$NOW" "$line" >> "$OUT/containers.tsv"; done

  printf '%s\t%s\n' "$NOW" "$(sample_gateway)" >> "$OUT/gateway.tsv"

  sleep "$INTERVAL"
done

# 存取日誌一次撈完：逐筆帶狀態碼與毫秒數，是唯一能算出速率與錯誤率的來源。
docker logs "$CSP_CONTAINER" --since "${TOTAL}s" 2>&1 | grep 'csp.access' > "$OUT/access.log" 2>/dev/null

echo "側錄結束 → $OUT" >&2
echo "接著跑： ./infra/capture/summarize-incident.sh $LABEL" >&2
