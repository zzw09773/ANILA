# Runbook: connection-pool / search concurrency load test

**目的**:驗證 RAG search 路徑在並發下是否再次把 pooled DB connection 跨外送
embedding HTTP 佔住（舊缺陷：池 30、實測 ~31 連線、health 全掛、負載停後
>8 分鐘才恢復）。

**不是**:端到端真實模型延遲、也不是「3000 人同時上線」的容量保證。

## 前提

- 本機可打到平台入口（預設 `https://127.0.0.1`，nginx → csp）。
- 內網 gateway（`aiagent2` / `aiops`）**不可達** → 必須用 throwaway stub。
- 可對 `anila` 送負載；**不要** recreate / 改 compose / 重啟其容器。
- 需要 `docker`（跑 `grafana/k6` 與 stub）、`curl`、`python3`。
- 密碼只走環境變數：`ANILA_PASSWORD`。

## 步驟

```bash
cd <repo>
export ANILA_PASSWORD=…
export ANILA_BASE_URL=https://127.0.0.1
export EMBED_DELAY_MS=500   # cliff 對照改 2000

./infra/loadtest/setup-stub.sh
# → 在 anila-net 起 stub、註冊 loadtest-embed-stub、設為 platform embedding

COLL=$(./infra/loadtest/seed-collection.sh 8)
# → 少量 markdown；DB 應只增加數 MB。若接近 512MiB 立刻停。

ANILA_COLLECTION_ID=$COLL \
  ./infra/loadtest/run-sweep.sh profile-search.js "1 2 4 8 16 24 32 48 64"

ANILA_COLLECTION_ID=$COLL ./infra/loadtest/measure-recovery.sh

./infra/loadtest/teardown-stub.sh
# 用量紀錄會擋住硬刪模型 → 腳本改為停用；stub 容器會刪掉。
```

對照組（量 stub 本身）:

```bash
docker run --rm --network host \
  -v "$PWD/infra/loadtest:/scripts:ro" \
  -e EMBED_STUB_URL=http://127.0.0.1:18088 \
  -e ANILA_VUS=32 -e ANILA_DURATION=15s -e ANILA_PASSWORD=x \
  grafana/k6 run /scripts/control-stub-direct.js
```

## 數字怎麼讀

| 欄位 | 意義 |
|---|---|
| `search_p50/p95_ms` | 單次 `POST .../search` 完成時間（含 stub 延遲） |
| `search_fail_rate` | 非 200 / 非 JSON results 的比例 |
| `http_5xx` | 5xx 次數（常見為 nginx 502） |
| `health_ms` | 該級結束後 `GET /health` 延遲；仍低但 API 大量 502 → 「容器 healthy、入口已掛」 |
| `pg_peak_total` | 負載中 `pg_stat_activity` 連線峰值 |
| `pg_peak_idle_in_xact` | `idle in transaction` 峰值；舊缺陷會在此堆高並貼近池上限 |

**池修復成立的判準（對照舊懸崖）**:

- 在 VU≈32、stub delay 足以讓舊 bug 佔住連線時，`pg_peak_total` **明顯低於 30**，
  `idle_in_xact` 不堆成「一筆 embed 一條連線」。
- `search_fail_rate≈0`，`/health` 維持低延遲。
- 負載停止後，`measure-recovery.sh` 數秒內恢復（舊版 >8 分鐘）。

**可接受延遲（本 runbook 假設）**: stub delay \(D\) 時，p95 ≤ \(D + 500\) ms。
超過代表平台自身開始排隊；`fail_rate>1%` 代表錯誤開始。

## 併發模型（對 3000 人）

3000 是註冊/可能使用者，不是 3000 concurrent。

假設尖峰同時在線 ~10%（300），其中正在打 RAG search 的 ~5–10% → **約 15–30
concurrent searches**。因此 VU=32 是舊懸崖與「合理尖峰」的對齊帶；VU=64+
用來找錯誤起點，不代表要支撐 64 個真人同時搜。

## 已知陷阱

1. **不要改 `nvidia/nv-embed-v2` 的 endpoint**：CSP 一重啟，`AUTO_REGISTER_MODELS`
   會蓋回 gateway URL。本套件改用獨立模型 + platform embedding。
2. **空 results 不一定是壞的**：若 chunks 的 `embedding_source_model` 為 NULL，
   而 search 帶了 platform model filter，ANN 會回空陣列；embed + DB 路徑仍有跑到。
3. **高 VU 的瞬間 502 洪水**：常見是 CSP 短暫不可達 / 被外力 recreate，nginx
   `max_fails` 把 upstream 標死，此時 `docker ps` 仍可能 healthy。先看
   `docker inspect` 的 `StartedAt` 是否在負載中跳動。
4. 祕密不進 git；`results/` 已 gitignore。
