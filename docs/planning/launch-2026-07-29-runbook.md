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

## 0.5 ⛔ 從 0 部署的實測阻礙(2026-07-28 隔離 project 實地演練所得)

我在本機用隔離的 compose project 從零跑了一次正式 `platform.yml`,以下每一條
都是**實際撞到的**,不是推測。依撞到的順序:

| # | 阻礙 | 現象 | 解法 |
|---|---|---|---|
| 1 | **`deploy-prod.sh` 拒絕在 feature 分支執行** | `deployment identity mismatch` | 變更必須先進 `prod-intranet-card`(見 §9) |
| 2 | **39 個必填環境變數**,其中 14 個是鎖定映像的 `sha256:` content ID | compose 直接不解析 | 見下方完整配方;映像 ID 來自氣隙匯出 |
| 3 | `nginx` 的 `depends_on` 指向被 profile 排除的服務 | `service "nginx" depends on undefined service "anilalm"` —— **整個專案無效,連 DB 都起不來** | **已修**(`20ed02f`) |
| 4 | 部署姿態斷言 fail-closed | `Refusing to start: ... posture mismatch` 並逐項列出 | 補齊卡片五項,見配方 |
| 5 | **必須掛一份 CRL** | `CARD_CRL_BUNDLE_PATH must be a mounted CRL file` | 見下方 ⛔ |
| 6 | 必須宣告 CRL 來源 | `CARD_CRL_SOURCE must name the offline sync source/owner` | 填離線同步來源/負責人 |
| 7 | 必須宣告憑證政策 OID | `CARD_REQUIRED_CERT_POLICY_OIDS is required` | 向 CSPKI 取得實際 OID |
| 8 | **CRL 必須由釘選的 CSPKI CA 簽發** | `card CRL bundle validation failed: CRL issuer 不在釘選 CA bundle` | **⛔ 無法繞過,見下** |

### ⛔ 今天唯一無法用設定解決的阻礙:CSPKI 的 CRL

第 8 條是硬的。我用自製 CA 產的測試 CRL **被正確拒絕** —— 平台要求 CRL 的簽發者
必須落在釘選的 `cspki_ca_bundle.pem` 裡。這代表:

**沒有一份有效的、由中科院 CSPKI 簽發的憑證撤銷清單,`prod-intranet-card` 的 csp
就不會啟動。** 這不是可以用旗標放寬的東西(放寬它等於讓被撤銷的卡還能登入),
也不是我能在程式碼裡替你解決的。

**這是今天要立刻去要的東西**,而且要同時拿到:
1. 有效的 CRL 檔(放進 `<repo>/share/pki/`,容器內是 `/etc/anila/pki/`)
2. 該 CRL 的**離線同步來源與負責人**(填 `CARD_CRL_SOURCE`)
3. 卡片憑證的**政策 OID** 與 **EKU OID**(填 `CARD_REQUIRED_CERT_POLICY_OIDS`
   / `CARD_REQUIRED_EKU_OID`)

如果 `.15` 上**已經有**一個在跑的 card 部署,那這些東西就已經在那台機器的 `.env`
與 `share/pki/` 裡了 —— **先去那台機器上抄,不要重新申請**。這是最快的路。

#### 具體怎麼取(2026-07-28 從 CA bundle 與驗證程式碼實查)

⚠ **`intranet-prod-v1.0.0.tar.gz` 裡沒有 CRL。** 我拆開看過:源碼包只含
`cspki_ca_bundle.pem`(信任錨,repo 本來就有),沒有任何 `.crl`。那個 bundle 是
6/14 打的,早於 CRL 這道要求,而且 CRL 本來就是會過期的產物,不會被打進映像包。

**發布點**(從中繼 CA 憑證的 CRL Distribution Points 讀出來):

```
http://repository.ncsist.org.tw/repository/CARL.crl
```

從本開發機解不到這個主機名(內網 DNS),**在 `.15` 上應該可以**:

```bash
curl -sS -o /tmp/CARL.crl http://repository.ncsist.org.tw/repository/CARL.crl
openssl crl -inform DER -in /tmp/CARL.crl -noout -issuer -lastupdate -nextupdate
```

⚠ **但 `CARL.crl` 很可能不是你要的那一份。** CARL = Certificate **Authority**
Revocation List —— 由 Root 簽發、列的是被撤銷的**CA**。而驗證程式碼要的是
**簽發卡片憑證的那個 CA 所簽的 CRL**:

```python
# card_auth.py:394-397
candidates = crls.get(issuer.subject.public_bytes(), [])
if not candidates:
    raise CardConfigError("CRL bundle 缺少憑證 issuer 的撤銷清單")
```

憑證鏈是 `CSPKI Root CA G1` → `中科院憑證管理中心 - G1` → 卡片,所以要的是
**中繼那張(`中科院憑證管理中心 - G1`)簽發的使用者 CRL**。它的發布點寫在
**卡片憑證自己**的 CDP 欄位裡,不在 CA bundle 裡 —— 拿一張實體卡的憑證出來看:

```bash
openssl x509 -in <某張卡的憑證>.pem -noout -text | grep -A 4 "CRL Distribution"
# 順便把政策 OID 也讀出來,那就是 CARD_REQUIRED_CERT_POLICY_OIDS 要填的值
openssl x509 -in <某張卡的憑證>.pem -noout -text | grep -A 3 "Certificate Policies"
```

參考:中繼 CA 自己宣告的政策 OID 是 `2.16.886.105.100003.0.3.1` / `.2` / `.3`。
卡片憑證通常會宣告其中之一。

**格式要求(會踩)**:

| 要求 | 出處 | 不符合的後果 |
|---|---|---|
| 必須是 **PEM**(`-----BEGIN X509 CRL-----`) | `card_auth.py:552` | DER 直接被當成「不含任何 PEM CRL」 |
| 可以把多份 CRL **串接**在同一個檔 | `card_auth.py:546-549`(依 issuer 建索引) | 可同時放 CARL 與使用者 CRL |
| 必須有 `nextUpdate` 且**尚未到期** | `card_auth.py:405-412` | 過期 → 卡片登入**全部失敗** |

DER 轉 PEM:

```bash
openssl crl -inform DER -in /tmp/CARL.crl -outform PEM -out share/pki/card-crl-bundle.pem
# 多份就 cat 起來
cat crl-user.pem crl-ca.pem > share/pki/card-crl-bundle.pem
```

⛔ **這是持續性的維運工作,不是一次性的。** `.env.example` 的
`CARD_CRL_MAX_AGE_HOURS=24` 加上程式對 `nextUpdate` 的檢查,代表 **CRL 一旦過期,
全院就登不進來**。開放前要先排好定期同步(氣隙環境就是排定期人工搬運),
否則平台會在某個沒人預期的早上整個登不進去 —— 而那個失效看起來會像平台壞了,
不像 CRL 過期。

### 完整 `.env` 配方(演練驗證過的最小集)

```bash
# 身分與路徑
ANILA_DEPLOYMENT_PROFILE=prod-intranet-card
ANILA_HOST=anila.ai.ncsist.org.tw
SITE_URL=https://anila.ai.ncsist.org.tw
ANILA_SECRETS_DIR=/opt/anila/secrets
ANILA_STATE_DIR=/opt/anila/state
ANILA_TLS_CERTS_DIR=/opt/anila/tls

# 14 個鎖定映像(sha256:...,由氣隙匯出產生)
ANILA_IMAGE_CSP=sha256:...
ANILA_IMAGE_INGESTION_WORKER=sha256:...
ANILA_IMAGE_ROUTER=sha256:...
ANILA_IMAGE_ANILA_UI=sha256:...
ANILA_IMAGE_ANILALM=sha256:...          # 即使不上線仍要填(compose 要解析)
ANILA_IMAGE_PPTX_RENDERER=sha256:...
ANILA_IMAGE_ANILA_STUDIO=sha256:...
ANILA_IMAGE_ANILA_AGENT=sha256:...
ANILA_IMAGE_ASR_GATEWAY=sha256:...
ANILA_IMAGE_CSP_DB=sha256:...
ANILA_IMAGE_REDIS=sha256:...
ANILA_IMAGE_NGINX=sha256:...
ANILA_IMAGE_CODESERVER=sha256:...       # 不啟動仍要填
ANILA_IMAGE_N8N=sha256:...              # 同上
ANILA_IMAGE_GITLAB=sha256:...           # 同上

# 密鑰(每個都要真值,dev 預設值會讓平台拒絕啟動)
ADMIN_PASSWORD=...            CSP_DB_PASSWORD=...        CSP_APP_DB_PASSWORD=...
CSP_SECRET_KEY=...            CSP_SERVICE_TOKEN=...      INTERNAL_PLATFORM_API_KEY=...
STUDIO_ARTIFACT_SERVICE_TOKEN=...       STUDIO_RUNTIME_SERVICE_TOKEN=...
STUDIO_JOB_ENVELOPE_HMAC_KEY=...        INGESTION_QUEUE_HMAC_KEY=...
EMBEDDING_MODEL_FINGERPRINT=...
CODESERVER_PASSWORD=...       N8N_ENCRYPTION_KEY=...     N8N_OWNER_EMAIL=...
N8N_OWNER_PASSWORD_HASH=...   GITLAB_ROOT_PASSWORD=...   GITLAB_SSH_BIND_IP=127.0.0.1

# 卡片姿態(prod-intranet-card 的斷言會逐項檢查,缺一不啟動)
ENABLE_CARD_LOGIN=true
REQUIRE_CARD_LOGIN_ONLY=true
ANILA_ALLOW_HTTP_AGENT_ENDPOINT=true     # MLSteam agent 走純 http NodePort
ANILA_ALLOW_HTTP_ENDPOINT=0              # ⚠ 這個必須維持 0
CARD_CRL_REQUIRED=true
CARD_INITIAL_OWNERS=<初始 owner 的卡號>
CARD_CRL_BUNDLE_PATH=/etc/anila/pki/<真的 CSPKI CRL>.pem
CARD_CRL_SOURCE=<離線同步來源/負責人>
CARD_REQUIRED_CERT_POLICY_OIDS=<CSPKI 政策 OID>
CARD_REQUIRED_EKU_OID=<CSPKI EKU OID>

# 連線池與逾時(2026-07-28 新接線,先前設了也不會生效)
ANILA_DB_POOL_SIZE=10
ANILA_DB_MAX_OVERFLOW=20
ANILA_DB_IDLE_TX_TIMEOUT_MS=330000       # 必須 > LLM_TIMEOUT×1000
LLM_TIMEOUT=300

# agent 時效(2026-07-28 新接線)
AGENT_TRACE_TEST_FRESHNESS_SECONDS=604800   # ⚠ 見 §10,預設 86400 = 24 小時就過期
ANILA_PILOT_MODE=false                       # ⚠ true 會讓 agent 清單直接回空
```

**啟動 stack 時不要帶任何 profile** —— `codeserver` / `n8n` / `gitlab` / `anilalm`
都在 profile 後面,不帶就不會起來,這是正確姿態。

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


---

## 9. 變更怎麼上到部署分支

`deploy-prod.sh` 的 `check_branch` 只接受 `prod-intranet-card` /
`prod-public-passwd` / `prod-military-passwd` / `trial-military`,在 feature
分支上直接 `deployment identity mismatch` 中止。所以今天的路徑是:

1. **PR #53 合入 `main`**(113 個 commit,CI 全綠)
2. **`main` → `prod-intranet-card`**。兩條分支目前**只差 `.env.example` 一個檔案**
   (24 行),所以這一步很乾淨。
   ⚠ **`.env.example` 的姿態值必須語意重推導,不可直接套舊 diff** ——
   以 main 現行範本為基底、只覆寫 card 分支蓄意的值(`ENABLE_PUBLIC_SHARE=false`、
   `ENABLE_MEMORY=false`、`REQUIRE_CARD_LOGIN_ONLY=true` 等)。07-22 曾因為
   直接 apply 舊 diff 而弄丟 card 的旗標。
3. 在 `prod-intranet-card` 上跑 `deploy-prod.sh`。

---

## 10. 既有 agent 要怎麼上線 —— 短答:**不改 agent 就上不了**

這是查證過的結論,不是推測:

- **註冊本身不需要 agent 做任何事** —— 是人(admin/developer)拿 JWT 打
  `POST /api/agents/register`,manifest 也可以由平台端代填,agent 不必架
  `.well-known` 端點。
- **但註冊 ≠ 可派工。** `approve` 對 `trace_test_passed_at` 是**硬閘、無 grandfather
  條款**,沒過就回 409。而 trace-test 的必過項包含「agent 主動把 6 種 span POST
  回 CSP」,**沒有任何旗標可以關掉**。
- 唯一能跳過 readiness 的 `ALLOW_LEGACY_AGENT_DISPATCH`,在任何 `prod-*` 姿態下
  設 true 會讓 **CSP 拒絕啟動**。
- **關鍵不對稱**:CSP 對 agent **只送 `X-CSP-Service-Token`,永遠不送
  `Authorization`**(刻意的:不把 gateway key 外流給第三方 agent)。所以只認
  `Authorization: Bearer` 的既有 agent 會回 401,在連線測試第一關就死。

### agent 端最小改動(兩件)

1. **收 `X-CSP-Service-Token`** —— 最低限度是「收到沒有 `Authorization` 的請求
   時不要回 401」。治理主控台發 `csk-` 的畫面旁邊就附了可直接貼的
   Starlette middleware(`apps/csp-governance-ui/src/components/agents/inboundGuardSnippets.js`)。
2. **回吐 6 種 span** 到 `POST {CSP}/v1/traces/{trace_id}/spans`,帶
   `Authorization: Bearer <該 agent 的 csk->` 並**原樣 echo** `X-ANILA-Task-Id`
   與 `X-ANILA-User-Id`(不符會 403)。必備:`agent.run.started/finished`、
   `agent.model_call.started/finished`、`agent.output.started/finished`,
   且父子關係要收斂到單一根。參考實作在
   `packages/anila-agent/anila_agent/tracing.py`。

### ⚠ 開放日的兩個地雷

- **trace-test 證據 24 小時就過期**(`AGENT_TRACE_TEST_FRESHNESS_SECONDS` 預設
  86400)。過期後 agent **靜默**變成不可派工 —— 沒有錯誤、沒有通知,只是從
  `GET /v1/agents` 消失。**今天做 trace-test、明天才開放,正好會踩到。**
  已接進 compose,`.env` 設 `604800`(7 天,上限)可避開。
- **`ANILA_PILOT_MODE` 必須是 `false`** —— true 時 `GET /v1/agents` 直接回空陣列,
  Router 一個 agent 都看不到。
