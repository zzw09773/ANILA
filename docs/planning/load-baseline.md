# ANILA 負載基線(W2-8)

**量測日期**:2026-07-27(修前與修後同日)
**量測者**:Claude(實作 B 通道)
**k6 腳本**:[`infra/loadtest/`](../../infra/loadtest/)

| 欄位 | 程式修訂版 | 內容 |
|---|---|---|
| **PRE-FIX(修前)** | `83bfdde` | 不含 W2-1 / W2-2 的對照組 |
| **POST-FIX(修後)** | `a035495` + 未提交的 `infra/loadtest/` | 含 W2-1 / W2-2,本次新增 |

---

## ⚠️ 讀這份文件之前必須知道的兩件事

### 1. 這些數字**不是**生產容量承諾

本機的 GPU、模型、gateway 都與 `.15` 內網部署**不同**(見下方 stack 規格)。
這裡的數字只有一個用途:**當作回歸比較的相對基準**——日後同樣的腳本、同樣的
stack 再跑一次,數字若明顯變差就是回歸。

**本文件不設及格線。** 容量是否足夠是 Gate 6 的驗收事項,不是 W2-8 的。
任何人拿這份文件的數字去宣稱「平台可以支撐 N 人」都是誤用。

### 2. 兩欄是**兩次獨立量測**,不是同一次跑出來的

PRE-FIX 欄的 worktree **不含** W2-1 與 W2-2;POST-FIX 欄在補救分支上重跑。
修改到位的證據(POST-FIX 樹):

| 錨點 | PRE-FIX (`83bfdde`) | POST-FIX (`a035495`) |
|---|---|---|
| `api/ingestion/documents.py` 的 `asyncio.to_thread` 次數(W2-2) | 0 | **4** |
| `services/retrieval_service.py` 的 `resolve_and_evaluate_data_access_batch`(W2-1 ①) | 0 | **3** |
| `tests/test_upload_zip_off_loop.py`(W2-2 測試) | 不存在 | **存在** |
| `app/database.py` 池參數(W2-1 ②) | `pool_size=10, max_overflow=20` 寫死,無 `pool_timeout`/`pool_recycle` | 走 env:`pool_size=10`、`max_overflow=20`、`pool_timeout=10s`、`pool_recycle=1800s` |
| PG 連線層 guard(W2-1 ②) | 無 | `lock_timeout=5000ms`、`idle_in_transaction_session_timeout=150000ms` |

⚠️ **兩次量測的差異(誠實標註,見 §6 未竟事項)**:

- **RAG 語料規模不同**:PRE-FIX 的 collection 有 3,412 份文件 / 2,884 chunks
  (其中大部分是前一輪無節流冒煙的殘留);POST-FIX 用 `seed-collection.sh 50`
  乾淨灌入 **50 份 / 300 chunks**。兩者的向量檢索成本都遠低於嵌入呼叫與 LLM
  串流,對本文件關心的**連線佔用失效模式**沒有影響,但**延遲絕對值不宜逐毫秒對比**。
- **POST-FIX 關掉了 `ENABLE_MEMORY`**(見 §1 stack 規格)。
- 上游 gateway 是**外部共用資源**,兩次量測之間它自身的負載可能不同。

---

## 1. Stack 規格

臨時隔離 stack,compose project = `anila-loadtest`,量完即拆。
**兩次量測的 stack 規格相同,除了下方標記 POST-FIX 的兩項。**

### 主機

| 項目 | 值 |
|---|---|
| CPU | 13th Gen Intel Core i7-13700K,24 threads,1 socket |
| RAM | 62 GiB |
| Kernel | 6.8.0-124-generic |
| Docker / Compose | 28.2.2 / v2.36.2 |
| 本機 GPU | NVIDIA RTX A4000 16 GiB — **本次量測未使用**(模型全在外部 gateway) |

⚠️ 兩次量測期間使用者的 `anila-platform-dev`(12–13 個容器)**都同時在跑**,
共用同一台主機的 CPU/RAM。這是本基線的已知噪音來源。

### 容器

| 服務 | 映像 | 版本 |
|---|---|---|
| csp | `anila-loadtest-csp`(本地建置) | Python 3.11.15 |
| csp-db | `pgvector/pgvector:pg16` | PostgreSQL 16.10,`vector` 0.8.1 |
| redis | `redis:7-alpine` | — |
| ingestion-worker | `anila-loadtest-ingestion-worker`(本地建置) | — |
| embed-proxy | `embedding-proxy:migration` | OpenAI→Triton gRPC 轉接 |

**未啟動**:nginx、router、anila-agent、anila-studio、anilalm、anila-ui、
asr-gateway、pptx-renderer。k6 直接打 csp 的 `127.0.0.1:18000`,不經 nginx——
量的是平台本身,不是 TLS 終結。

CSP 容器**未設** `mem_limit` / CPU 限制,即可用滿主機資源。

**POST-FIX 專有的兩項設定差異**(都寫在 `infra/loadtest/compose.loadtest.yml`,有註解):

1. **`ENABLE_MEMORY=false`**。`infra/compose/dev.yml` 預設 `true`,而在補救分支上
   memory recall **綁定 consuming conversation 才放行**
   (`memory_service.py:1136`)。k6 直接打 `/v1/chat/completions`(沒有
   conversation),於是**每一個 RAG 請求都會在檢索之前就 503**
   (`memory governance stage failed,已依 fail-closed 拒絕出向呼叫`)。
   memory recall 與 W2-1/W2-2 無關,關掉也正好是 prod-intranet-card 的姿態。
2. **`embed-proxy` 需要註冊為 trusted host**。出向 SSRF guard 擋 single-label
   主機名,健康探測因此打不到 `http://embed-proxy:8000`,模型被標成
   `unhealthy`,proxy 斷路器再以 503 `model_unhealthy` 拒絕所有嵌入呼叫,
   文件全部 `[E_EMBED_MODEL_DOWN]` 失敗。`seed-collection.sh` 已自動補這一步。

### 模型後端

| 角色 | 來源 |
|---|---|
| 聊天 | 外部 OpenAI 相容 gateway 上的 `gemma26-nothink`(litellm 前端) |
| 嵌入 | 外部 Triton gRPC,`nv-embed-v2`,原生 4096 維 → 平台截斷存成 `halfvec(4000)` |

**兩者都是外部共用資源,不在本機。** 影響見 §5 的 gateway 對照組。
⚠️ gateway 的 `endpoint_url` 要填**不含 `/v1`** 的 base(proxy 自己接
`/v1/chat/completions`);填了會變 `/v1/v1/...` → 404 → 空串流。

---

## 2. 量到的到底是什麼

k6 沒有逐塊讀 SSE 的 API,所以每次串流都以一般 POST 發出,取 k6 原生的兩個時間:

- **`anila_ttft_ms`** = `res.timings.waiting`,第一個 response byte 的時間。
  ⚠️ 本平台會**先送一個 meta/role frame**,所以這是「第一個 SSE frame」,
  **不是嚴格的第一個內容 token**。做回歸比較有效,但別當成使用者感知的首字延遲,
  **也不要拿它跟 gateway 對照組的 TTFT 直接相比**(對照組沒有那個前導 frame,
  §5 有實例)。
- **`anila_stream_complete_ms`** = `res.timings.duration`,到串流關閉為止。
  受 `max_tokens` 支配,所有 profile **一律固定 128**——這個值不可以改。

**成功判準不看 status code。** SPA catch-all 對未匹配路由會回 200 `text/html`。
判準是:`Content-Type: text/event-stream` **且** body 含 `data:` frame **且**
以 `data: [DONE]` 收尾。profile 2 額外要求出現 `event: anila.retrieval` frame——
沒有就是**靜默的 RAG 失敗**,不計成功。

**飽和點怎麼找**:每個併發等級是**獨立的一次 constant-vus 執行**(45 秒),
不是一條 ramp。ramp 會把各等級的 p95 糊在一起。

---

## 3. 重現步驟

前置:未追蹤的 repo 根 `.env`(鍵列表見
[`infra/loadtest/README.md`](../../infra/loadtest/README.md);**祕密只放這裡**)。

```bash
# 0) 先驗隔離 —— 這步不可以跳。
docker compose -f compose.dev.yaml -f infra/loadtest/compose.loadtest.yml \
  -p anila-loadtest config | grep -E 'name:|anila-dev-net|:5533'
#    必須看到 name: anila-loadtest,且不得出現 anila-dev-net 或 :5533

# 1) 拉起隔離 stack
docker compose -f compose.dev.yaml -f infra/loadtest/compose.loadtest.yml \
  -p anila-loadtest up -d --build csp ingestion-worker embed-proxy

# 2) 健康檢查(從 host 打 published port;csp 容器內沒有 curl)
curl -sS -w '\n%{content_type}\n' http://127.0.0.1:18000/health

# 3) 灌 RAG 語料(順便註冊 embed-proxy 為 trusted host + 補 clearance)
export ANILA_PASSWORD='<.env 的 ADMIN_PASSWORD>'
COLL=$(./infra/loadtest/seed-collection.sh 50)

# 4) 五個 profile(POST-FIX 欄就是這樣跑出來的)
./infra/loadtest/run-sweep.sh profile1-chat-sse.js "1 2 4 8 16 32"
ANILA_COLLECTION_ID=$COLL ./infra/loadtest/run-sweep.sh profile2-rag-chat.js "1 2 4 8 16 32"
ANILA_UPLOAD_PACE=1.0 ./infra/loadtest/run-sweep.sh profile3-upload-index.js "1 2 4 8 16 32"
./infra/loadtest/make-zip-fixtures.sh 40 60
./infra/loadtest/run-sweep.sh profile3b-upload-zip.js "1 2 4 8"
ANILA_GATEWAY_URL=<base,不含 /v1> ANILA_GATEWAY_KEY=<token> \
  ./infra/loadtest/run-sweep.sh control-gateway-direct.js "1 2 4 8 16 32"

# 5) RAG 的連線證據(§4.3):跑 profile 2 的同時另開一條
./infra/loadtest/pg-sample.sh rag-vu4 70 3

# 6) 拆掉
docker compose -p anila-loadtest down -v
docker ps -a --filter name=anila-loadtest    # 必須是空的
```

每級 45 秒。結果 JSON 落在 `infra/loadtest/results/`(已 gitignore)。

---

## 4. 結果

### 4.0 總表

| Profile | 飽和併發 PRE → POST | 飽和點 p95 PRE → POST | 修後結論 |
|---|---|---|---|
| 1 純聊天 SSE | 1 → 1(皆未觸及 ANILA 天花板) | 902ms → 904ms @1VU | 不變;天花板屬 **gateway**(§5) |
| 2 RAG 聊天 | **2(4 崩潰且不恢復)→ 16(32 才降級,且會恢復)** | 2506ms @2VU → 32.5s @16VU | **W2-1 生效**(§4.3) |
| 3 上傳+索引(單檔) | >32(未飽和)→ >32(未飽和) | canary 82.5ms → 103.6ms @32VU | 不變,無回歸 |
| 3b 上傳+索引(ZIP) | **4 → >8(ramp 內未飽和)** | canary 873.8ms @4VU → **192.8ms @4VU** | **W2-2 生效**(§4.7) |
| 對照 gateway 直打 | 16–32 之間 → 16–32 之間 | 99.68% → 99.59% 錯誤 @32VU | gateway 限流,兩次一致 |

**兩個 W2-8 規格點名要回答的問題:**

1. **RAG 還會不會在 4 併發鎖死整個平台?→ 不會。** 4 VU 錯誤率
   **11.76% → 0.00%**,吞吐量 **0.11 → 0.77 iter/s(7 倍)**,而且
   **每一級跑完 `/health` 都是 200 / 1.8ms**(修前 8 分鐘後仍 timeout)。
   降級點推到 **32 VU**,失效模式從「死鎖不恢復」變成「排隊變慢後自行恢復」。
2. **ZIP 在相同受理速率下的 canary p95?→ 22.7 zip/s 時 192.8ms**
   (修前 22.88 zip/s 時 873.8ms),**改善 4.5 倍**;8 VU 更是
   3287ms → 354ms(**9.3 倍**),且吞吐量不再倒退。

---

以下各表:延遲單位 ms(標 s 者為秒),`—` = 該 profile 不產生該指標。
每級 45 秒、獨立執行。

### 4.1 Profile 1 — 純聊天 SSE

| VUs | iter/s PRE → POST | TTFT p95 PRE → POST | 串流完成 p95 PRE → POST | 錯誤率 PRE → POST |
|---|---|---|---|---|
| 1 | 1.44 → 1.35 | 16.9 → 15.5 | 901.9 → 904.5 | 0.00% → 0.00% |
| 2 | 1.64 → 1.63 | 19.9 → 14.7 | 1714.1 → 1710.0 | 0.00% → 0.00% |
| 4 | 1.65 → 1.44 | 40.3 → 19.9 | 3161.3 → 3410.0 | 0.00% → 0.00% |
| 8 | 1.49 → 1.50 | 111.3 → 79.1 | 6303.7 → 6790.0 | 0.00% → 0.00% |
| 16 | 1.56 → 1.55 | 204.4 → 159.4 | 11721.2 → 11760.0 | 0.00% → 0.00% |
| 32 | — → (4.34)\* | 305.1 → 252.7 | 21198.3 → 21820.0 | 0.30% → **62.45%**\* |

**飽和併發 = 1(兩次皆然)**:吞吐量從 1 VU 起就沒再成長(全程 ~1.5 iter/s),
延遲幾乎與 VU 數成正比。這是「服務率已達上限、增加併發只是在排隊」的教科書型態。
⚠️ **但這條天花板不是 ANILA 的**,見 §5。

\* 32 VU 那格兩次都不是吞吐量數字,而是 gateway 硬性限流下的**快速失敗**。
修後的 62.45% 比修前的 0.30% 難看,但**同一天同一時段的 gateway 對照組是 99.59%
失敗**(§5)——也就是說 32 VU 時 gateway 幾乎全滅,ANILA 反而還擋下了一部分。
**這一格反映的是外部 gateway 當下的負載,不是 ANILA 的回歸。**

### 4.2 Profile 2 — RAG 聊天

- PRE-FIX 語料:3,412 份文件 / 2,884 chunks
- POST-FIX 語料:50 份文件 / 300 chunks(`seed-collection.sh 50`)

| VUs | iter/s PRE → POST | TTFT p95 PRE → POST | 串流完成 p95 PRE → POST | Task 建立 p95 PRE → POST | 錯誤率 PRE → POST | 空檢索 |
|---|---|---|---|---|---|---|
| 1 | 0.53 → **1.25** | 1251.3 → **120.2** | 2378.1 → 1080.0 | 14.6 → 12.5 | 0.00% → 0.00% | 0% → 0% |
| 2 | 0.81 → **0.99** | 1362.8 → **105.6** | 2505.5 → 2670.0 | 118.7 → **10.4** | 0.00% → 0.00% | 0% → 0% |
| 4 | **0.11 → 0.77** | 4279.0 → **181.1** | 6906.3 → 6470.0 | 1180.3 → **26.9** | **11.76% → 0.00%** | 0% → 0% |
| 8 | **0(崩潰)→ 0.69** | — → 377.3 | — → 13080.0 | — → 63.2 | — → **0.00%** | — → 0% |
| 16 | **0(崩潰)→ 0.54** | — → 771.0 | — → 32470.0 | — → 213.0 | — → **0.00%** | — → 0% |
| 32 | **0(崩潰)→ 0.18** | — → 147s | — → 151s | — → 6.85s | — → 70.00% | — → 0% |

**飽和併發:2 → 16。** 修前 8/16/32 三級的「0 iteration」是 k6 `setup()` 逾時被
中止——平台在前一級 4 VU 就已經卡死(§4.3)。修後 **1→16 VU 全程 0% 錯誤、
0% 空檢索**,32 VU 才降級成 70% 逾時。

⚠️ 修後 32 VU 的 TTFT 147 秒代表**請求在排隊**(池 30 條、`pool_timeout=10s`、
串流各自佔用),不是鎖死——見下節的連線證據與 `/health`。

### 4.3 ⛔→✅ 最重要的發現:RAG 併發鎖死已解除

**修前**(節錄,完整見本節末):RAG 在 **4 VU** 就讓整個平台**永久卡死**,
停止負載 8 分鐘後 `/health` 仍 timeout,只有 `docker restart` 能救。
根因:`retrieval_service.py:232-252` 的 `embed_query` 在
`await proxy_request(...)` **兩側各夾一條 session**(`policy_db`、`governance_db`),
加上請求自己的 `get_db()` = **每個 RAG 請求跨 await 釘住 3 條連線**;上游 429 時
這些 session 沒被關掉,永遠停在 `idle in transaction` 並持有 `model_registry`
列鎖,擋住健康檢查的 `UPDATE`,再擋住**每一個要讀 `model_registry` 的聊天請求**。

**修後的程式狀態**:`policy_db` 在 `await` **之前**就 `close()`
(`retrieval_service.py:254-262`,附註解說明 `governance_db` 因為要傳進
`proxy_request` 記治理帳而**不能**關)。因此
**每請求跨 await 釘住的連線從 3 條降為 2 條**。
另外連線層新增 `idle_in_transaction_session_timeout=150s` 與 `lock_timeout=5s`,
即使真的有 session 卡住也會被 PG 主動回收,不再是永久的。

**實測連線證據**(`pg-sample.sh`,每 3 秒取樣一次,每級 23 個樣本,涵蓋負載期
與其後的殘留期):

| VU 等級 | 連線總數峰值 | `idle in transaction` 峰值 | 該狀態最長持有 | 出現阻塞鏈的樣本數 |
|---|---|---|---|---|
| 1 | 10 | 2 | **42 ms** | **0** |
| 2 | 6 | 2 | **42 ms** | **0** |
| 4 | 10 | 2 | **71 ms** | **0** |
| 8 | 13 | 2 | **15 ms** | **0** |
| 16 | 19 | 0 | — | **0** |
| 32 | 39 | 32 | 68 s | **0** |

**怎麼讀這張表:**

- **`idle in transaction` 峰值 = 2,不是 3** —— 與「`policy_db` 提前 close」
  的程式改動一致,直接回答了 W2-8 問的「是否仍每請求釘住 3 條」:**否**。
- **持有時間從「8 分 13 秒且永不釋放」變成「15–71 毫秒」**(1 至 8 VU)。
  這才是修好的本體:session 不再跨越那個網路呼叫。
- **`pg_blocking_pids` 在 138 個樣本中全部為空** —— 修前那條
  「`SELECT model_registry` → 擋住 `UPDATE model_registry` → 擋住全部聊天」
  的鎖鏈**一次都沒有再形成**。
- **32 VU 那列是排隊,不是鎖死**:32 條 `idle in transaction`(每 VU 一條)、
  總連線 39 < 池上限 30+overflow,持有 68 秒是**串流本身還開著**;
  且該級跑完 `/health` 仍是 **200 / 1.8ms**。

**`/health` 在每一級跑完後的實測**(修前 4 VU 之後即永久 timeout):

| VU | 剛跑完 | 冷卻 20s 後 |
|---|---|---|
| 1 | 200 / 1.8ms | 200 / 3.5ms |
| 2 | 200 / 1.7ms | 200 / 1.9ms |
| 4 | 200 / 1.8ms | 200 / 1.7ms |
| 8 | 200 / 1.8ms | 200 / 1.8ms |
| 16 | 200 / 1.8ms | 200 / 1.8ms |
| 32 | 200 / 1.8ms | 200 / 1.8ms |

**結論:W2-1 達成它宣稱要達成的事。** 失效模式從
「4 併發鎖死、不重啟不會好」變成「32 併發時排隊變慢、負載一停就恢復」。

<details>
<summary>修前的原始證據(保留供對照)</summary>

4 VU RAG 之後直接查 `pg_stat_activity`:

```
state               | count | max held
--------------------+-------+-----------
active              |     4 | 00:08:11
idle                |     7 | 00:08:16
idle in transaction |     2 | 00:08:13     ← 元凶
```

阻塞鏈(`pg_blocking_pids`):

```
2249 (idle in transaction, SELECT model_registry …)   ← 根源
  └─ 擋住 1477 (UPDATE model_registry SET health_checked_at=…)
       └─ 擋住 2340 (SELECT model_registry …)
2341 (idle in transaction, SELECT model_registry …)   ← 根源
  └─ 擋住 2332 (SELECT ingestion_collections …)
       └─ 擋住 2334 (SELECT ingestion_collections …)
```

連線總數只有 13,遠低於池上限 30 ——**不是池被用完,是鎖死**。
停止所有負載 8 分鐘後 `/health` 仍 timeout;只有
`docker restart anila-loadtest-csp-1` 能救回來。容器健康檢查顯示 `unhealthy`
但**不會**被重啟(`restart: unless-stopped` 不看 healthcheck)。

</details>

### 4.4 上游 429 的處理仍有缺陷(兩次都在,**未修**)

負載中 csp 兩次都記錄了成串的
`fastapi.exceptions.HTTPException: <code>: 下游回應錯誤`,每一次都緊接著:

```
RuntimeError: Caught handled exception, but response already started.
```

SSE 的 header 送出去之後才發現上游錯誤,已無法改回應狀態碼,於是**用戶端收到
一條沒有 `[DONE]`、沒有任何錯誤說明的截斷串流**。修後在 profile 1 @ 32 VU 的
62.45% 失敗串流大多是這一類。**這不在 W2-1/W2-2 範圍內,仍待處理**
(與 W2-12 的結構化錯誤信封同一族)。

### 4.5 Profile 3 — 上傳 + 索引(單檔 `.md`)

每個 VU 每秒上傳一份 ~40KB markdown(`ANILA_UPLOAD_PACE=1.0`),
同時一條固定 2 rps 的 canary 打 `GET /api/ingestion/collections`。

| VUs | iter/s PRE → POST | 上傳受理 p95 PRE → POST | **canary p95 PRE → POST** | 錯誤率 |
|---|---|---|---|---|
| 1 | 2.96 → 2.96 | 28.2 → 20.1 | **6.5 → 5.3** | 0% → 0% |
| 2 | 3.90 → 3.95 | 30.7 → 24.8 | **6.1 → 31.7** | 0% → 0% |
| 4 | 5.73 → 5.76 | 85.9 → 77.0 | **27.7 → 22.2** | 0% → 0% |
| 8 | 9.43 → 9.39 | 95.7 → 118.0 | **34.3 → 45.6** | 0% → 0% |
| 16 | 16.70 → 16.32 | 132.1 → 161.7 | **77.5 → 104.2** | 0% → 0% |
| 32 | 31.03 → 30.99 | 134.8 → 172.9 | **82.5 → 103.6** | 0% → 0% |

**兩次都到 32 VU 都沒有飽和**,吞吐量幾乎完全線性且**逐格吻合**
(31.03 vs 30.99 iter/s),錯誤率全程 0%。修後 canary 略高(82.5 → 103.6ms,
同一量級)——單檔端點本來就不是 W2-2 改的地方,這格的作用是**證明沒有回歸**。

⚠️ **這裡的「上傳」只是受理(202 + 入佇列),不是索引完成。**
修後實測受理速率 32 VU 時 **29.04 份/s**(修前 29.07),而索引消化速率修前實測
**1.85–2.12 份/秒**——**受理比消化快一個數量級,佇列會無上限成長**。
這是容量規劃的真實限制,但不是本包要修的東西。

### 4.6 Profile 3b — ZIP 上傳(W2-2 真正的路徑)

> **為什麼要分開一支**:W2-2 修的是 `upload_zip`
> (`POST /api/ingestion/collections/{id}/documents/zip`),它的 per-member 迴圈
> (讀 + 驗 + sha256 + 落地)修前跑在 `async def` 內。**profile 3 走單檔端點,
> 根本碰不到那段迴圈。** 3b 用 40 個各含 60 個成員的壓縮包補上。

這一支的重點指標是 **canary**,不是上傳時間:同步的 per-member 迴圈卡住的是
**整條 event loop**,症狀是「別人的請求也一起凍住」。

| VUs | 受理 zip/s PRE → POST | zip 受理 p95 PRE → POST | **canary p95 PRE → POST** | 改善 |
|---|---|---|---|---|
| 1 | 16.65 → 18.52 | 285.5 → 99.8 | **518.0 → 16.7** | **31.0×** |
| 2 | 20.29 → 17.27 | 128.8 → 541.4 | **727.8 → 100.1** | **7.3×** |
| 4 | 22.88 → 22.73 | 261.5 → 202.1 | **873.8 → 192.8** | **4.5×** |
| 8 | 20.38 → **24.80** | 400.5 → 340.1 | **3287.3 → 354.5** | **9.3×** |

**飽和併發 4 → ramp 內未飽和。** 修前 8 VU 吞吐量**倒退**(22.88 → 20.38 zip/s)
且 canary 暴衝到 3.3 秒;修後吞吐量**持續成長**(22.73 → 24.80 zip/s),
canary 只到 354ms。1 至 8 VU 全程 0% 錯誤,每級跑完 `/health` 都是 200。

**與單檔上傳在相近受理速率下的對照(這才是 W2-2 的正題):**

| | 受理速率 | canary p95 | 每秒同步處理的成員數 |
|---|---|---|---|
| 單檔(profile 3 @32VU)PRE | 29.07 份/s | 82.5ms | 29 |
| **ZIP(profile 3b @4VU)PRE** | 22.88 包/s | **873.8ms** | ~1373 |
| 單檔(profile 3 @32VU)POST | 29.04 份/s | 103.6ms | 29 |
| **ZIP(profile 3b @4VU)POST** | 22.73 包/s | **192.8ms** | ~1364 |

**修前:受理速率更低,canary 卻差 10.6 倍。修後:同樣的比較只差 1.86 倍。**
event loop 阻塞的代價下降到原本的 **1/5.7**。剩下的 1.86 倍是 zip 端點本來就
比單檔端點做更多工作(每包 60 個成員的 CPU 與磁碟 I/O 仍然存在,只是搬到了
`asyncio.to_thread` 的執行緒池,不再霸佔 event loop)。

⚠️ 誠實標註:profile 3 有 1 秒節流、3b 沒有,兩者**每 VU 到達率不同**。
上表刻意改用 `accepted/s` 對齊來繞開這個差異,但這仍是**兩支不同腳本的比較**。
**同一支腳本的 PRE vs POST(上面那張 3b 表)才是最乾淨的證據**,而它顯示
canary 在每一個 VU 等級都改善 4.5–31 倍。

---

## 5. 對照組:直接打 gateway(路徑中沒有 ANILA)

`control-gateway-direct.js` 用**完全相同**的請求形狀(同模型、同
`max_tokens=128`、同 `temperature=0`)直接打上游 gateway,繞過 ANILA。

| VUs | gateway iter/s PRE → POST | profile 1 iter/s POST | gateway 串流完成 p95 PRE → POST | profile 1 串流完成 p95 POST | gateway 錯誤率 PRE → POST |
|---|---|---|---|---|---|
| 1 | 1.57 → 1.46 | 1.35 | 871.9 → 877.6 | 904.5 | 0.00% → 0.00% |
| 2 | 1.66 → 1.57 | 1.63 | 1703.1 → 1700.0 | 1710.0 | 0.00% → 0.00% |
| 4 | 1.63 → 1.63 | 1.44 | 3394.3 → 3390.0 | 3410.0 | 0.00% → 0.00% |
| 8 | 1.48 → 1.77 | 1.50 | 6788.2 → 5680.0 | 6790.0 | 0.00% → 0.00% |
| 16 | 1.51 → 1.57 | 1.55 | 11488.9 → 11960.0 | 11760.0 | 0.00% → 0.00% |
| 32 | (508)\* → (411)\* | (4.34) | 21573.5 → 21400.0 | 21820.0 | **99.68% → 99.59%** |

\* 32 VU 那格的 iter/s 不是吞吐量:gateway 在 32 併發直接硬性限流,幾乎每個請求
都**立刻**收到 429。真正的意義是 **gateway 的併發上限在 16 與 32 之間**,
**兩次量測完全一致**。

**結論(兩次相同):profile 1 量到的天花板 100% 是 gateway 的,不是 ANILA 的。**
gateway 自己在 1 VU 就吐不快(~1.5 iter/s),加併發只會排隊。兩者的串流完成 p95
逐格吻合到 ±5%。

**因此:**
- **ANILA 純聊天路徑的額外成本 ≈ 30ms 或更低**(以 1 VU 的乾淨比較為準:
  877.6 vs 904.5ms)。
- **ANILA 純聊天的真正併發天花板,兩次都沒有量到**——外部 gateway 先飽和了。
  要量它得換一個吞吐量遠高於平台的模型後端(或 mock 模型)。**仍列為未完成事項。**

⚠️ **不要拿對照組的 TTFT 跟 profile 1 的 TTFT 相比**:ANILA 會先送一個
`anila.meta` 前導 frame,所以 16 VU 時 profile 1 的 TTFT 是 159ms 而對照組是
11.2 秒——那不代表 ANILA 比 gateway 快 70 倍,只代表兩者「第一個 byte」的語意
不同。**可比的是 `anila_stream_complete_ms`。**

⚠️ 反過來說,**profile 2 修前的崩潰不能用 gateway 解釋**:gateway 在 4 VU 時
0% 錯誤、吞吐量穩定,而同樣 4 VU 的 RAG 修前是 11.76% 錯誤 + 平台卡死。
**那是 ANILA 自己的問題,而且修後它消失了。**

---

## 6. 未完成 / 未確認事項

1. **ANILA 純聊天的真正併發天花板未量到**(gateway 先飽和)。需要 mock 模型後端。
2. **兩次量測的 RAG 語料規模不同**(2,884 vs 300 chunks)。對失效模式無影響,
   但 RAG 延遲的絕對值不宜逐毫秒對比。要完全乾淨,需在同一語料上重跑 PRE-FIX。
3. **`ENABLE_MEMORY` 修後被關掉**(修前該樹不需要)。memory recall 打開後的
   RAG 熱路徑成本**未量測**。
4. **索引消化速率修後未重新取樣**(沿用修前的 1.85–2.12 份/秒)。
   受理/消化的數量級落差是既有的容量議題,W2-8 不負責修。
5. **上游 429 的截斷串流(§4.4)未修**,修後仍在。
6. profile 1 @ 32 VU 的 62.45% 失敗**歸因於 gateway 當下負載**(同時段對照組
   99.59% 失敗),但**未以第三次量測交叉驗證**。

---

## 附錄:量測過程中發現的平台行為

這幾條不是效能數字,但會擋住任何想重跑這套測試的人。

1. **上傳完成的終態是 `indexed`,不是 `completed`**(`processing_stage` 才是
   `complete`)。輪詢等 `completed` 會永遠等不到。
2. **RAG 一定要先建 Task 並帶 `X-ANILA-Task-Id`**,否則 422。
3. **一個 Task 只能密封一次檢索快照**——同一個 task_id 打第二次 RAG 會 409
   `snapshot_already_sealed`。所以**每次 RAG 查詢都要一個新 Task**。
4. **沒有 clearance grant 的話,上傳會靜默失敗**:API 照樣回 202,但 worker 一律
   拒絕,文件全部變 `failed`。要先 `POST /api/clearance/grants` 再
   `POST /api/clearance/grants/{id}/collections/{collection_id}`。
5. **`K6_VUS` / `K6_DURATION` 是 k6 的保留環境變數**,設了會讓 k6 **整段捨棄
   `scenarios` 設定**。腳本因此改用 `ANILA_VUS` / `ANILA_DURATION`。
6. **compose 的 `ports:` 是附加不是取代**。overlay 若不加 `!override`,
   csp-db 會同時掛 5533(使用者 dev stack 正在用)與新 port。
7. **(修後新增)模型 `endpoint_url` 必須是不含 `/v1` 的 base**,
   否則 proxy 接成 `/v1/v1/chat/completions` → 404 → **200 + 空 SSE body**。
8. **(修後新增)single-label 主機名(如 compose 服務名 `embed-proxy`)會被出向
   SSRF guard 擋掉**,健康探測失敗 → 模型標 `unhealthy` → 斷路器 503。
   要 `POST /api/trusted-hosts` 註冊。
9. **(修後新增)`ENABLE_MEMORY=true` 時,沒有 conversation 綁定的
   `/v1/chat/completions` 會被 memory 治理 fail-closed 擋成 503**,
   RAG 完全跑不起來。純 API 客戶端的負載測試要關掉它。
10. **(修後新增)worker 的 `EMBEDDING_API_KEY` 與 CSP 的 `AUTO_SEED_API_KEYS`
    都讀 `INTERNAL_PLATFORM_API_KEY_DEV`**;只設 `INTERNAL_PLATFORM_API_KEY`
    會讓兩邊對不上 → 嵌入 401 → 文件全部 `[E_EMBED_MODEL_DOWN]` 失敗。
