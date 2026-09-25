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

入口：治理中心 `:443`、對話 `/anila/`。Composer 的 accessible name 是「傳訊息給 ANILA」。

## 內部服務身分（自動核發）

CSP 在啟動時，以及之後每個週期（預設一小時），為設定名單裡的內部服務核發憑證。預設只有 `router-primary`（類型 `router`）。名單是 `ANILA_INTERNAL_SERVICE_CLIENTS` 的 JSON；留空就用預設，要加 `ingestion-worker` 時把那筆加進 JSON。`router-primary` 一律會核發，名單漏掉它、或把它寫成別的類型，仍然以 router 核發。明文只寫進 `/run/anila/service-clients/<client_name>.token`（mode 0640，群組 `anila-svc-tokens` gid 10002），日誌不記明文。chmod／chown 失敗，或寫完之後的 mode／gid 不符，這次發布算失敗，readiness 降級。約 30 天輪替一次，上一把在寬限期（預設 24 小時）內仍可通過驗證。`ANILA_SERVICE_CLIENT_AUTO_PROVISION=0` 時 `/health` 是 503，`status=degraded`、`service_client_provisioning=disabled`。

Router 讀 `ANILA_SERVICE_TOKEN_FILE`。檔案變了會重讀（另外每 30 秒看一次 mtime）；CSP 回 401／403 時再讀一次才放棄。路徑有設而檔案不在時 `token_source=file_missing`，不改用別的憑證，並繼續重讀，CSP 寫上檔就恢復。`/health` 的 `token_source` 還有 `file`、`file_error`、`state_file`、`bootstrap`、`legacy_env`、`none`。state 檔、`CSP_BOOTSTRAP_TOKEN`、`CSP_SERVICE_TOKEN` 只在 `ANILA_SERVICE_TOKEN_FILE` 沒設時才是後援。`CSP_SERVICE_TOKEN` 仍給其他服務當舊式共用祕密，不再是 Router 的正常憑證。

緊急吊銷：治理中心「服務客戶端」按吊銷。CSP 不會把已吊銷的列重新核發，並刪掉憑證檔，該服務因此失敗即關閉。日誌可搜 `refusing to re-issue`。要恢復時，刪掉那筆已吊銷的 `service_clients` 列，然後重啟 CSP（或等下一個週期）；系統會重新核發並寫檔。畫面上的手動輪替只供緊急使用，新憑證會直接寫回憑證檔，不必貼進 `.env`。

換上這版之後要做一次：重建 csp 與 router 映像（兩邊都加了 gid 10002），再用更新後的 compose 啟動，讓新的 named volume `anila-service-credentials`（dev 是 `anila-service-credentials-dev`）掛上。不要再把 `CSP_BOOTSTRAP_TOKEN` 灌進 router。

## GitLab（2026-09-26 先拿掉）

compose 不再宣告 `gitlab` 服務，也不再宣告 `gitlab_config`、`gitlab_logs`、`gitlab_data`。nginx 各 listener 不再代理 `/gitlab`。部署腳本不再寫 `GITLAB_*`。n8n 與 code-server 仍在。

這次沒有刪除主機上的 Docker volume。舊的 `anila-platform_gitlab_data` 還在主機上，之後由擁有者自行移除。

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
