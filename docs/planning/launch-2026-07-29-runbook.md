# 全院開放 runbook —— 2026-07-29

> **背景**:院長緊急下令對全院(≥3000 人)開放,個人知識庫(ANILALM)不在範圍內。
> 上級指示是「爆就讓它爆」,用可見的失效證明算力不足、據以爭取資源。
>
> **本檔的前提**:那個策略只有在平台**為了正確的理由而死**時才成立。
> 死在 nginx 限流、死在 PostgreSQL 砍 session、死在日誌塞爆磁碟 —— 這三種
> 失效產生的證據講的都是「工程沒做好」,不是「算力不足」,而且會反過來變成
> 否決要求的理由。所以下面的準備工作不是在「搶救效能」,是在**把所有不是
> 算力不足的死法拆掉,讓剩下唯一的死因無可爭辯**。

---

## 0. 三十秒版

| 何時 | 做什麼 |
|---|---|
| **今晚** | ① 部署本分支 ② 驗設定真的進了容器 ③ 跑一次上游 gateway 探測 ④ 決定要公告哪個網址 |
| **開放當下** | 開側錄:`./infra/capture/capture-incident.sh launch-am 15 14400` |
| **結束後** | `./infra/capture/summarize-incident.sh launch-am`,填「瓶頸歸屬」那五格 |

**不要做的事**:不要在今晚把平台搬到 `.12`(理由見 §5)。不要為了「撐住」
去調參數到沒量過的值 —— 目的不是撐住,是取得乾淨的數據。

---

## 1. 今晚:部署與驗證

```bash
# 在 .15 上,正式 stack
docker compose -p anila-platform -f infra/compose/platform.yml up -d
```

**接著必驗**——這些設定本來就是因為「寫了但沒進容器」才出問題的,不要假設它生效:

```bash
docker compose -p anila-platform exec csp env | grep -E 'ANILA_DB_|LLM_TIMEOUT'
```

應該看到:

```
ANILA_DB_POOL_SIZE=10
ANILA_DB_MAX_OVERFLOW=20
ANILA_DB_POOL_TIMEOUT_S=10
ANILA_DB_IDLE_TX_TIMEOUT_MS=330000
LLM_TIMEOUT=300
```

⚠ **`ANILA_DB_IDLE_TX_TIMEOUT_MS` 必須大於 `LLM_TIMEOUT`×1000。** 這個不變式
先前是破的(150s vs 300s),後果是耗時 150–300 秒的推論會被 PostgreSQL 砍掉
session,使用者看到 5xx —— 那看起來像 bug,不像塞車。CSP 啟動時會檢查並在
違反時記 error log:

```bash
docker compose -p anila-platform logs csp | grep 'ANILA_DB_IDLE_TX_TIMEOUT_MS'
# 沒有輸出 = 通過
```

nginx 設定是 bind mount,不需重建映像:

```bash
docker compose -p anila-platform exec nginx nginx -t && \
docker compose -p anila-platform exec nginx nginx -s reload
```

---

## 2. 今晚:唯一真正決定明天的量測

**負載基線裡的天花板數字,量的是外網那台 gateway,不是 `.12`。**
內網 `.12` 的模型端點**從來沒有被壓測過**,而它才是明天的實際瓶頸。

```bash
ANILA_GATEWAY_URL=https://aiagent2.ai.ncsist.org.tw/v1 \
ANILA_GATEWAY_KEY=<.12 的 API key> \
ANILA_CHAT_MODEL=<gpt-oss-20b 或 gemma4 的實際模型名> \
ANILA_PASSWORD=<admin 密碼> \
./infra/loadtest/run-sweep.sh control-gateway-direct.js "1 2 4 8 16 32"
```

這支**把 ANILA 從路徑上拿掉**,直接打模型端點。它會告訴你 `.12` 在幾個併發
開始拒絕請求 —— **那個數字就是明天的上限**,平台端怎麼調都不會超過它。

有了這個數字,要資源的話講的就不是「它掛了」,而是「上游在 N 個併發就飽和,
而尖峰需求是 M,缺口是 M/N 倍」。**後者才提得出具體採購規格。**

---

## 3. 今晚:要公告哪個網址

平台有兩個前端各自由不同東西提供:

| 網址 | 提供者 | 給誰 |
|---|---|---|
| `https://<host>/anila/` | **獨立的 nginx 容器**,靜態檔完全不經過 Python | **全院使用者 → 公告這個** |
| `https://<host>/` | csp 的 Python 行程(治理主控台 SPA) | 管理員 |

⚠ **一定要公告 `/anila/`。** 裸網址 `/` 落在治理主控台,而那是由**單一 Python
行程**用 anyio 的 40 條執行緒池送檔案 —— 三千人同時開首頁會讓靜態檔案傳輸
去跟 API 請求搶同一個執行緒池。這是純粹自傷,而且它會污染數據(看起來像平台
撐不住,其實是叫錯網址)。

刻意**不**加 `/` → `/anila/` 的轉址:那會打斷管理員進治理主控台,開放當天
弄壞管理入口的風險比公告網址高得多。

---

## 4. 開放當下:側錄

```bash
cd <repo>
ANILA_GATEWAY_URL=https://aiagent2.ai.ncsist.org.tw/v1 \
ANILA_GATEWAY_KEY=<key> \
ANILA_CHAT_MODEL=<model> \
./infra/capture/capture-incident.sh launch-am 15 14400   # 每 15 秒一筆,共 4 小時
```

全程唯讀,不重啟任何東西。可以隨時 Ctrl-C,已寫入的資料都保留。

**為什麼一定要跑**:這個平台**沒有 `/metrics` 端點**,沒有延遲或錯誤率指標。
`/api/usage` 是 token 用量的商業分析,回答不了「請求在哪裡塞住」。負載一停,
`pg_stat_activity` 就歸零,容器一重啟日誌就沒了。**不側錄,事後誰都補不回來,
手上就只剩一句「它壞了」。**

結束後:

```bash
./infra/capture/summarize-incident.sh launch-am
# → infra/capture/results/launch-am/summary.md
```

---

## 5. 要不要搬到 `.12`(EPYC 9554)?

**今晚不要搬。** EPYC 9554(64C/128T、Zen 4)確實遠強於 E5-2698 —— 單執行緒
約 2–2.5 倍、核心數 3–4 倍,而平台是單行程單事件迴圈、253 個路由裡有 213 個
是同步處理函式,單執行緒效能對它很敏感。**但四個理由指向今晚不動:**

1. **量到的瓶頸不在平台 CPU。** 32 VU 時 `/health` 仍然 200 / 1.8ms —— 事件
   迴圈當時**沒有**飽和。瓶頸在上游模型端點(16–32 併發)。把平台換到更快的
   機器,不會把那個天花板往上推。
2. **`.12` 正在跑模型。** 平台搬過去就跟推論搶 CPU;而推論才是真正的稀缺
   資源。用比較不稀缺的資源去擠比較稀缺的,方向是反的。
3. **會弄糊證據。** 平台與模型同機之後,「算力不足」變成「哪一種算力?」
   分開放,「上游飽和而平台自身資源沒滿」這個論述乾淨得多。
4. **開放前夜搬遷本身就是最大的單一風險。** 失敗會是部署失誤造成的,而那種
   失效換不到任何資源。

**什麼情況我會改口**:如果 `.12` 上的模型是跑在 GPU 上、CPU 大部分閒置,那麼
開放**之後**把平台搬過去很有吸引力 —— 除了 CPU,還省掉每個請求對 `.12` 的
一次完整 TCP+TLS 握手(目前 httpx client 是**每請求新建**、沒有連線重用,
450 併發就是 450 次同時握手)。**那是下一輪的事,不是今晚。**

---

## 6. 已經拆掉的「假死因」(供事後對照)

| 原本會怎麼死 | 為什麼那是壞證據 | 現況 |
|---|---|---|
| nginx 刷卡驗證限流 2r/s、`limit_conn 20`,且鍵是真實 TCP 對端(無 `real_ip_header`)。若使用者經 NAT 出來,那是**全院合計**,3000 人登入排 25 分鐘拿裸 503 | **一次推論都沒發生**,只證明登入做不好 | 已抬到 100r/s;限流改回 **429**(與上游 503 區分) |
| PostgreSQL 在 150 秒砍掉還在等模型的 session | 使用者看到 5xx,讀起來像 bug 不像塞車 | 預設抬到 330s + 啟動時檢查不變式 |
| Docker 日誌無輪替、無上限成長 → 磁碟滿 → PG 停止寫入 | 平台以「軟體壞掉」的姿態死去,且日誌本身也沒了 | 每服務 100m × 5 |
| 連線池寫死 10+20,運維改 `.env` 無效 | 撞到池上限會被讀成「平台限制」而非算力不足 | 六個參數接進 compose,可調 |

## 7. 仍然存在、但**刻意不修**的(明天會看到)

這些是真缺陷,但都不是「假死因」——它們不會讓平台為了錯誤的理由死:

- **連線池耗盡沒有例外處理**:池抽乾後 `sqlalchemy.exc.TimeoutError` 直接變成
  裸 500;SSE 情境更糟,會是**沒有 `[DONE]`、沒有錯誤說明的截斷串流**。修它要
  動 SSE 的錯誤路徑(標頭已送出),不是開放前夜該做的手術。
- **重試放大**:`PROXY_MAX_RETRIES=3`,上游飽和時每個使用者請求最差會產生
  約 901 秒的上游工作量,而治理交易在整個重試迴圈期間保持開啟。若明天觀察到
  連線佔用遠高於預期,這是第一個要查的地方。
- **無任何配額或併發上限**:`ErrorCode.QUOTA_EXCEEDED` 存在但**零個拋出點**。
  沒有 admission control,450 個請求會直接衝進一個容量 32 的後端。

---

## 8. 不受「讓它爆」涵蓋的事

「爆」是容量策略,**不延伸到資料暴露面**。容量失效可以復原、而且對爭取資源
有用;資料外洩不能復原,而且會反過來成為停用平台的理由。

- **ANILALM 不上線這件事同時關掉了四個已實證的跨 surface 缺陷**:搜尋會回傳
  別的 app 的對話、無機密內文 snippet 進了回應主體、替使用者**沒做過**的探測
  寫永久稽核列(`audit_logs` 於 `r1_0041` 後 append-only,刪不掉)、以及 by-id
  直接回 HTTP 200。**這四條全部需要 ANILALM 的資料才觸發** —— 所以請確保
  ANILALM 真的沒有被啟動,這不只是「少一個功能」。
- **資料密等門檻不變**:依 roadmap §6.1,目前僅允許**無機密**受控 pilot。
  開放範圍變大不改變這條。
- `ENABLE_PUBLIC_SHARE` 與 `ENABLE_MEMORY` 在 `platform.yml` 預設皆為 `false`,
  維持原樣。
