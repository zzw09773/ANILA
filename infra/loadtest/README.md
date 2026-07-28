# ANILA 負載測試(k6)— W2-8

三個 profile,量測 ANILA 平台在**臨時隔離 stack** 上的延遲與飽和併發數。
數字本身記在 [`docs/planning/load-baseline.md`](../../docs/planning/load-baseline.md)。

> ⚠️ **這裡的數字是相對值,供回歸比較用,不是生產容量承諾。**
> 本機的 GPU/模型與 `.15` 不同,不設及格線(那是 Gate 6 的事)。

## 檔案

| 檔案 | 用途 |
|---|---|
| `profile1-chat-sse.js` | 純聊天串流(SSE)。CSP 認證 → model registry → 計量 → 串流 proxy。 |
| `profile2-rag-chat.js` | RAG 聊天。額外含 Task 建立、query 嵌入、向量檢索、逐文件 clearance 解析、快照密封。 |
| `profile3-upload-index.js` | 上傳 + 索引(單檔),並行一條 canary 探針量測 event loop 是否被卡住。 |
| `profile3b-upload-zip.js` | **ZIP 上傳——W2-2 真正的程式路徑**。profile 3 走單檔端點,碰不到 `upload_zip` 的 per-member 迴圈。 |
| `control-gateway-direct.js` | **對照組**:同樣的請求直接打上游 gateway,繞過 ANILA。用來判斷 profile 1/2 的天花板是 ANILA 的還是 gateway 的。 |
| `lib/common.js` | 登入、SSE 判讀、共用 options。 |
| `seed-collection.sh` | 建立並灌好一個 collection 給 profile 2 用(含必要的 clearance grant)。 |
| `make-zip-fixtures.sh` | 產生 profile 3b 要用的壓縮包(產物 `fixtures/` 已 gitignore)。 |
| `run-sweep.sh` | 逐一併發等級跑同一個 profile,找飽和點(每級之間有冷卻)。 |
| `pg-sample.sh` | 負載中取樣 `pg_stat_activity`(狀態計數、最長交易、阻塞鏈)。W2-1 的連線佔用證據靠它。 |

## 鐵則(照 `CLAUDE.md` §5 與 W2-8 spec)

- **只打自己拉起來的 `anila-loadtest` stack**。禁止打 `anila-platform` /
  `anila-platform-dev`(使用者的環境)與 `.15` / 任何 `*.ncsist.org.tw`。
- **祕密不進這個目錄**。密碼與 gateway token 一律走環境變數;
  stack 自己的 key 放未追蹤的 repo 根 `.env`。

## 拉起隔離 stack

`compose.dev.yaml` 內含 `name: anila-platform-dev`,而 `infra/compose/dev.yml`
把預設網路寫死成 `anila-dev-net`(**其擁有者是使用者正在跑的 dev project**)。
因此**一定要疊 `compose.loadtest.yml`**,它負責改網路名、改容器名、換掉會撞的
host port。細節見該檔頂端註解。

未追蹤的 repo 根 `.env` 需提供:

```
EMBEDDING_MODEL_FINGERPRINT_DEV=sha256:<64 hex>
EMBEDDING_MODEL_FINGERPRINT=sha256:<同上>
ANILA_DEV_TLS_CERTS_DIR=<repo 外的目錄>
MODEL_GATEWAY_API_KEY=<外部 gateway 的 bearer>
LOADTEST_GATEWAY_BASE=<http://host:port  ← 不含 /v1>
LOADTEST_CHAT_MODEL=<聊天模型名>
LOADTEST_EMBED_GRPC=<host:port,嵌入模型 gRPC>
ADMIN_PASSWORD=<種子 admin 密碼>
# worker 的 EMBEDDING_API_KEY 與 CSP 的 AUTO_SEED_API_KEYS 都讀這一個。
# 只設 INTERNAL_PLATFORM_API_KEY 會讓兩邊對不上 → 嵌入 401 → 文件全部失敗。
INTERNAL_PLATFORM_API_KEY_DEV=<sk-...,兩邊共用>
```

其餘 `:?` 必填變數(`ANILA_IMAGE_*`、各種 token、`ANILA_STATE_DIR` …)compose
會在 interpolation 階段就要求,即使那些服務不啟動也一樣;丟暫時值即可,但
`STUDIO_ARTIFACT_SERVICE_TOKEN` / `STUDIO_RUNTIME_SERVICE_TOKEN` / `CSP_SERVICE_TOKEN`
**必須是 `csk-` 開頭**,否則 csp 啟動時 lifespan 就 raise。

⚠️ `LOADTEST_GATEWAY_BASE` 要**不含 `/v1`**:proxy 自己接
`/v1/chat/completions`,填了會變 `/v1/v1/...` → 404 → **200 + 空 SSE body**
(看起來像平台壞了,其實是設定)。

```bash
docker compose -f compose.dev.yaml -f infra/loadtest/compose.loadtest.yml \
  -p anila-loadtest up -d --build csp ingestion-worker embed-proxy
```

**up 之前先驗隔離**(這步不要跳):

```bash
docker compose -f compose.dev.yaml -f infra/loadtest/compose.loadtest.yml \
  -p anila-loadtest config | grep -E 'name:|anila-dev-net|5533'
# 必須看到 name: anila-loadtest,且不得出現 anila-dev-net 或 5533
```

## 跑 profile

```bash
export ANILA_PASSWORD='<admin 密碼>'

# profile 1
./infra/loadtest/run-sweep.sh profile1-chat-sse.js "1 2 4 8 16 32"

# profile 2(先灌資料,拿 collection id)
COLL=$(./infra/loadtest/seed-collection.sh 5)
ANILA_COLLECTION_ID=$COLL ./infra/loadtest/run-sweep.sh profile2-rag-chat.js "1 2 4 8 16"

# profile 3(單檔)
./infra/loadtest/run-sweep.sh profile3-upload-index.js "1 2 4 8 16 32"

# profile 3b(ZIP = W2-2 路徑;先造壓縮包)
./infra/loadtest/make-zip-fixtures.sh 40 60
./infra/loadtest/run-sweep.sh profile3b-upload-zip.js "1 2 4 8"

# 對照組:直接打 gateway,判斷天花板歸屬
ANILA_GATEWAY_URL=<base,不含 /v1> ANILA_GATEWAY_KEY=<token> \
  ./infra/loadtest/run-sweep.sh control-gateway-direct.js "1 2 4 8 16 32"
```

⚠️ **修 W2-1 之前**,profile 2 會把平台弄到卡死(見 load-baseline.md §4.3),
跑完 RAG 那組後必須 `docker restart anila-loadtest-csp-1` 才能跑下一組。
**W2-1 之後(`a035495` 起)已不需要**:實測 1–32 VU 每級跑完 `/health` 都是
200 / 1.8ms。若又出現需要重啟的情況,那本身就是回歸。

RAG 的連線證據跟 profile 2 併行取樣:

```bash
./infra/loadtest/pg-sample.sh rag-vu4 70 3 &   # 與該級 k6 同時開跑
```

## 收工

```bash
docker compose -p anila-loadtest down -v
docker ps -a --filter name=anila-loadtest   # 必須是空的
```

## 量到的到底是什麼

k6 沒有逐塊讀 SSE 的 API,所以每次串流都是一般 POST,用 k6 原生的兩個時間:

- `anila_ttft_ms` = `res.timings.waiting`,即**第一個 response byte**。本平台
  上游 gateway 會先送一個 role frame,所以這個值是「第一個 SSE frame」而不是
  嚴格的「第一個內容 token」——比較回歸時仍然有效,但別當成使用者感知的首字延遲。
- `anila_stream_complete_ms` = `res.timings.duration`,串流關閉為止,受
  `max_tokens` 上限支配(三個 profile 都固定 128,不要改,改了就不能跟舊數字比)。

成功與否**不看 status code**:SPA catch-all 對未匹配路由會回 200 text/html。
判準是 `Content-Type: text/event-stream` **且** body 有 `data:` frame **且**
以 `data: [DONE]` 收尾。profile 2 另外檢查有沒有 `event: anila.retrieval`
frame——沒有就代表檢索什麼都沒帶進來,那是**靜默的 RAG 失敗**,不能算成功。
