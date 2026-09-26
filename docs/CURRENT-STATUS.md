# 目前狀態（給交接與代理）

> 這一頁才是「現在這棵樹怎麼跑」。歷史細節在 `PLAN.md`、`docs/office/`、`docs/anila-redesign-docs/`。
> 更新：2026-09-26。HEAD 以 `git log -1` 為準。

## 開發線

- 分支：`main`（單一開發線；舊七分支模型已進 attic，不要再照 `AGENTS.md` §3 切 `prod-intranet-card` 那種線）
- 專案權威：`PLAN.md`（現況與順序）、`SYSTEM-MAP.md`（規格）
- `CLAUDE.md` **不存在**。環境事實看本頁與 `PLAN.md`，不要去找那份檔。

## 啟動與測試

```bash
# 平台（repo 根目錄）
docker compose up -d --build

# Shell 主流程（Vitest orchestrator，不是已刪的 Playwright functions.spec.js）
cd apps/anila-shell && npm test

# 治理前端
cd apps/csp-governance-ui && npm test && npm run build
```

入口：治理中心 `:443`、對話 `/anila/`、知識庫與 Studio `/anilalm/`。Composer 的 accessible name 是「傳訊息給 ANILA」。

## ANILA LM（2026-09-26 開放）

知識庫與 Studio（`apps/anilalm`）對使用者開放。四道閘門都是開的：nginx 443／4443 把 `/anilalm` 轉給 anilalm、Shell「我的知識庫」可點、治理中心儀表板卡片可見、`POST /api/services/{id}/launch` 不再對 ANILA LM 回 503。再關閉的方法見 `docs/runbooks/anilalm-release-gate.md`。

已經在跑的容器不會因為改這棵樹自動換上。要讓線上入口跟著開，照那份 runbook 重建 `anila-ui`、`csp`，並 force-recreate nginx。

Shell 對話列不再放「產出」按鈕。那個連結只帶對話 Task 的 id；Shell 建立的 Task 是 `source_scope=none`、沒有知識庫，對應對話的 `collection_id` 也是空的，ANILA LM 沒有可開的工作區，Studio 綁定無從預填。

## 內部服務身分（自動核發）

CSP 在啟動時，以及之後每個週期（預設一小時），為內建名單核發憑證。人不產生、也不複製這些明文。

| 消費者 | 檔案 | 種類 | 群組 |
|---|---|---|---|
| router | `/run/anila/service-clients/router-primary/token` | 服務憑證 `csk-`（`client_type=router`） | 目錄 gid 10002 `anila-svc-tokens` |
| anila-studio | `/run/anila/service-clients/anila-studio/token` | 服務憑證 `csk-`（`client_type=studio`） | 目錄 gid 10003 `anila-studio-tokens` |
| ingestion-worker | `/run/anila/service-clients/ingestion-worker/token` | 系統使用者的 `sk-` API key（雜湊存在 `api_keys`，不是 `service_clients`） | 目錄 gid 10004 `anila-worker-tokens` |

子目錄的擁有者是 uid 10005（沒有服務用這個 uid），mode 2770，只有該群組進得去。CSP、studio、worker 都是 uid 10001；若憑證放在同一個他們擁有的目錄，0640 擋不住互讀。`csp-credential-dirs` 在 CSP 啟動前用 root 把這三個目錄建好（既有 volume 也不會漏），並刪掉根目錄的扁平 `<client>.token`。有刪到檔案時留下標記，CSP 把 `router-primary` 輪替一次且不留寬限，複製走的舊檔因此失效。沒有專屬目錄時 CSP 拒絕發布、不退回扁平檔，`/health` 降級。檔案本身是 0640。CSP 加入上述三個群組才能寫；每個消費者只加入自己的群組。上層目錄仍是 gid 10002、mode 2750。日誌不記明文。chmod／chown 失敗，或寫完之後的 mode／gid 不符，這次發布算失敗，readiness 降級。約 30 天輪替一次。服務憑證的上一把在寬限期（預設 24 小時）內仍可通過驗證；worker 的舊 key 同樣留到寬限期，但若那把是環境變數裡的 `INTERNAL_PLATFORM_API_KEY`，換發當下就停用。`ANILA_SERVICE_CLIENT_AUTO_PROVISION=0` 時 `/health` 是 503。

三個服務都讀 `ANILA_SERVICE_TOKEN_FILE`。檔案變了會重讀；CSP 回 401／403 時再讀一次才放棄。路徑有設而檔案不在或讀不到時，不改用別的憑證。`/health`（worker 沒有 HTTP，啟動日誌與 `credential_health()`）的 `token_source` 是 `file`、`file_missing` 或 `file_error`。studio 在 `file_missing`／`file_error` 時 `/health` 是 503。

自動核發開啟時（正式環境的預設），舊的共用 `CSP_SERVICE_TOKEN` 不再是任何服務身分，就算資料庫列上還留著那把祕密也一樣。這不靠環境變數裡還有沒有那把祕密。長效 `agent_credentials` 已退役（代理用 5 分鐘派工 JWT）；遷移 `r1_0048` 撤銷仍有效的列並清掉寬限複本，驗證路徑也不再接受那些列。自動核發關掉時，測試仍可用環境變數後援。

`asr-gateway` 的 compose 不再注入 `CSP_SERVICE_TOKEN`。程式裡的舊讀取路徑還在，重新啟用前必須改讀專屬憑證檔。本機生圖服務已刪除，不再讀 `INTERNAL_PLATFORM_API_KEY`。

緊急吊銷服務憑證：治理中心「服務客戶端」按吊銷。CSP 不會把已吊銷的列重新核發，並刪掉憑證檔。要恢復時，刪掉那筆已吊銷的 `service_clients` 列，然後重啟 CSP（或等下一個週期）。worker 的 key 不在那個畫面：把名為 `ingestion-worker-system-key` 的 API key 停用後，CSP 不會再核發，並刪掉憑證檔；要恢復就刪掉那些已停用的 key 列再重啟 CSP。

### 換上這版之後，擁有者要從 `.env` 刪掉的行

部署並確認 studio、worker、router 的 `token_source=file` 之後，刪掉這些行（整行，含值）：

- `CSP_SERVICE_TOKEN=...`

worker 已經不讀 `INTERNAL_PLATFORM_API_KEY`、`EMBEDDING_API_KEY`、`VISION_API_KEY`、`RELATION_LLM_API_KEY`。本機生圖服務也不再讀它。

同時要重建 csp、router、anila-studio、ingestion-worker 映像（群組 10002／10003／10004），再用更新後的 compose 啟動。憑證 volume 仍是 `anila-service-credentials`（dev 是 `anila-service-credentials-dev`）。不要再把 `CSP_BOOTSTRAP_TOKEN` 灌進 router。

`.env.example` 已拿掉的鍵：`CSP_SERVICE_TOKEN`。

## 生圖（2026-09-26：不部署本機模型）

治理中心「模型角色」多了「生圖模型」（`image_generation`，類型用既有的 `image`）。有設且健康時，Studio 經 CSP `POST /v1/images/generations` 配圖，不直連模型主機。沒設、不健康或請求失敗時，簡報仍用版面、圖示、圖表、表格，以及知識庫文件裡已有的圖，不留空的配圖框。

本機 `flux2-dev`／`flux2-dev-agent`、compose 服務、`/uploads/flux` 與 `FLUX_*` 環境變數已從這棵樹拿掉。這次沒有刪除主機上的 Docker volume。若先前起過 Studio，具名 volume `anila_anila-studio-flux-cache` 與 `anila-platform-dev_anila-studio-flux-cache-dev` 可能還在，由擁有者自行移除。`share/uploads/flux` 若還在磁碟上，同樣先留著。

## GitLab（2026-09-26 先拿掉）

compose 不再宣告 `gitlab` 服務，也不再宣告 `gitlab_config`、`gitlab_logs`、`gitlab_data`。nginx 各 listener 不再代理 `/gitlab`。部署腳本不再寫 `GITLAB_*`。n8n 與 code-server 仍在。

這次沒有刪除主機上的 Docker volume。舊的 `anila-platform_gitlab_data` 還在主機上，之後由擁有者自行移除。

## 外部服務（2026-09-26）

文件解析（Docling）與語音辨識的位址在治理中心「外部服務」，不在 `.env`。

1. 以管理員打開治理中心的「外部服務」。
2. 文件解析：填遠端 Docling 的位址、需要的話填憑證、打開啟用。不要把帳密寫進網址。沒啟用時，畫面寫明擷取走內建原生解析器。啟用之後服務中斷，擷取工作會失敗，不會改回原生解析器。
3. 語音辨識：填遠端解碼器位址、選 native 或 openai、憑證可留空、打開啟用。健康由 CSP 背景探測，畫面只顯示上次結果，不會因為重新整理就把憑證送出去。Shell 與 ANILA LM 只在這一列是啟用且健康時顯示麥克風。頁面載入與回到視窗時各問一次，未啟用時不會去打解碼器。
4. 要讓瀏覽器連得到語音串流，平台還要帶 `--profile asr` 把 asr-gateway 拉起來。gateway 只負責切句與轉送，位址向 CSP 讀。沒有本機 whisper。
5. 若部署前 `.env` 裡還有 `DOC_PARSER=docling` 與 `DOCLING_URL`，CSP 第一次啟動會匯入一次（日誌只記主機名）。確認畫面有位址之後，從 `.env` 刪掉 `DOC_PARSER`、`DOCLING_URL`、`DOCLING_SERVICE_TOKEN`，以及 `ASR_DECODE_URL`、`ASR_DECODER_TOKEN`、`ASR_DECODE_PROTOCOL`、`ASR_DECODE_API_KEY`、`ASR_OPENAI_MODEL`。指到 `asr-decoder` 這個舊本機名字的位址不會匯入。

憑證存在 CSP 自己的金鑰檔裡，不是模型 API key 那把 `SECRET_KEY`。畫面只看得到「有沒有憑證」。語音憑證只有 asr-gateway 讀得到，文件解析憑證只有 ingestion-worker 用它的憑證檔讀得到。

## 尚未當成上線完成的項目

- P2.6 打 tag／重打包
- Q53 人資 Oracle（`csiih.vihbuy`）等資安放行 `oracledb` wheel
- G9 對話密等標記介面
- `app.jsx`／`router_server.py` 大檔拆分（等主流程測試穩定後再抽）
- 真模型負載測試（現有 loadtest 是 stub embedding）

## 本輪工程債（2026-09-18 抽查）

1. Shell／治理測試基線（composer accessible name、群組 `extractError`）
2. 共用工作站資料夾 localStorage 依帳號隔離
3. 檢索 `calibrated` 必須對應當下 embedding 模型
4. DB 備份不可在多個 `csp-db` 時猜第一個；復原清單含附件
