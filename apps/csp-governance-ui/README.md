# ANILA 治理中心 — CSP Governance Console（`csp-platform`）

> CSP（Control / Security Plane）的治理控制台（Vue 3 + Vite，v1.0.0）。這是 ANILA 的 **Admin / Developer / Service Admin 控制面**，管理身份、模型、Agent Registry、Service Registry、知識治理、機敏分類與單向閂鎖，以及 Trace / Audit / Usage。**它不是一般使用者的日常入口**。

> English mirror: [`README.en.md`](./README.en.md)

- 平台現況見 [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md)。

---

## 1. 產品定位

治理中心是 **Admin-facing 控制面**，承載以下治理域：

```text
治理中心（CSP）
├── 身份 / 部門 / 角色
├── 模型治理（Model Gateway）
├── Agent Registry（上架審批）
├── Service Registry（GUI 服務註冊 + Launch）
├── 知識治理（Collections / Chunking）
├── 機敏分類與單向閂鎖
└── Trace / Audit / Usage
```

一般使用者在 ANILA Shell 裡工作，不進治理中心。owner、admin、developer，以及單位管理員，從 Shell 看得到入口。

---

## 2. 視覺

預設是淺色。色票在 `src/assets/styles/tokens.css`：`:root` 為淺色，`[data-theme="dark"]` 為藍灰。`src/composables/useTheme.js` 以淺色為預設（`localStorage` 鍵 `anila.theme`；沒有存過、而且作業系統是深色時才用深色）。`index.html` 在 Vue 掛載前用 inline script 設 `data-theme`，避免第一屏先深後淺。

字型用系統字（`--font-sans` 給介面，`--font-mono` 只給 ID、token、時間戳、數值），不下載外部字型。`index.html` 的 `lang` 是 `zh-TW`。

`src/components/cli/` 的 `Term*` 元件（Badge、Field、Modal、Stat）是共用 UI kit。名稱沿用舊的，畫面是淺色。

### 登入（card-first）

`src/views/LoginView.vue` 以**自然人憑證卡（PKI）為主要 hero 卡片**：偵測卡片 → 輸入 PIN → 卡片簽章（`handleDetectCard` / `handleCardLogin`）。本地帳密與 OIDC SSO 收合為次要「其他登入方式」，降低視覺權重。

---

## 3. Views 盤點（`src/router/index.js` 為權威）

路由分兩層：`/login`（public）與 `/`（`AppLayout`，`requiresAuth`）下的子路由。守衛 `beforeEach` 依 `stores/auth.js` 的角色分級（`isAdmin` = admin∪owner；`isOwner`；`isDeveloper` = developer∪admin∪owner）。

| 治理域 | View（route） | 重點 |
|---|---|---|
| 儀表板 | `DashboardView`（`/`） | 平台總覽（`dashboard/PlatformCard.vue`） |
| 模型治理 | `ModelsView`（`models`） | **五態健康**（`utils/healthStatus.js`：未知 / 健康 / 降級 / 異常 / 已停用，含舊 online/connecting/offline 正規化）＋**每模型金鑰**（`has_api_key`：已設定模型金鑰 / 使用全域金鑰；`api_key` write-only）。Model Gateway |
| Agent Registry | `DeveloperAgentsView`（`developer/agents`, developer）＋ `DeveloperGuideView` | **三態審批**（`utils/approvalStatus.js`：已註冊 / 已核准 / 已停用）。無七態、無軌跡測試關卡。測試連線探針仍在。 |
| Service Registry | `PlatformLinksView`（`platform-links`）、`ServiceAccessView`、`ServiceClientsView` | 已註冊 GUI 服務（`utils/serviceRegistry.js`：`launch_mode` 新分頁／iframe、`config_source` env_seeded／db 欄位鎖定、`classification_ceiling` 四級：無機密／營業秘密／密／機密）；service-token 管理。Service Registry |
| 知識治理 | `KnowledgeCollectionsView`、`ChunkingPreviewView`、`CollectionDetailView`（developer） | collection 檢視、chunking 策略比較精靈；關聯圖走 `components/RelationGraph.vue`（cytoscape） |
| 身份 / 部門 | `UsersView`、`DepartmentsView`（admin） | 使用者、部門、角色 |
| 稽核 / 用量 | `AuditLogsView`、`UsageView` | 稽核；用量以 echarts（`charts/UsageLineChart.vue`、`TimeRangeSelector.vue`） |
| 平台雜項 | `ApiKeysView`、`AlertsView`、`BannersView`（admin）、`TrustedHostsView`（admin, SSRF allow-list） | 金鑰、告警、公告、SSRF 信任主機清單 |

---

## 4. 技術棧（讀自 `package.json`）

| 類別 | 內容 |
|---|---|
| 框架 / 路由 / 狀態 | **Vue 3.5.13** · `vue-router` 4.5.0 · `pinia` 2.3.0 |
| 圖表 | `echarts` 5.6.0（`>=5.6.0 <6`）+ `vue-echarts` 7.0.3（`>=7.0.3 <8`）；關聯圖 `cytoscape` 3.34.0 |
| HTTP | `axios` 1.7.9 |
| 樣式 | `tailwindcss` 3.4.17 + `postcss` + `autoprefixer`（build 期）；設計 token 走 `src/assets/styles/tokens.css` |
| Build | **Vite 6.0.5**（`@vitejs/plugin-vue` 5.2.1） |

`scripts`：`dev` / `build` / `preview` / `test`（`node --test tests/*.test.mjs`）。utils（`healthStatus` / `approvalStatus` / `serviceRegistry`）是純函式，測試直接讀原始碼。

---

## 5. 目錄結構

```
apps/csp-governance-ui/
├── index.html（lang=zh-TW、data-theme=light、pre-mount 主題 script）· vite.config.js · tailwind.config.js · postcss.config.js
└── src/
    ├── main.js（createApp + pinia + router + main.css）· App.vue · router/index.js
    ├── views/          # 見 §3 盤點（+ LoginView.vue）
    ├── components/
    │   ├── layout/     # AppLayout / AppHeader / AppSidebar / AppStatusBar
    │   ├── cli/        # Term* 共用 UI kit（Badge / Field / Modal / Stat / Section …）
    │   ├── charts/     # UsageLineChart / TimeRangeSelector（echarts）
    │   ├── agents/     # AgentGuardPanel / BootstrapHowToTabs / bootstrapSnippets / inboundGuardSnippets
    │   ├── dashboard/  # PlatformCard
    │   └── RelationGraph.vue（cytoscape）
    ├── api/            # 見 src/api/：agents / models / usage / auditLogs / apiKeys /
    │                   #   services / serviceClients / serviceAccessGrants / platformLinks /
    │                   #   trustedHosts / ingestion* / users / departments / banners / alerts /
    │                   #   caAuth / chunkingPreview / client
    ├── stores/         # apiKeys / auth / models / usage（pinia）
    ├── utils/          # approvalStatus（3 態）/ healthStatus（5 態）/ serviceRegistry
    ├── composables/    # useTheme（淺色優先）/ useDialog
    └── assets/styles/  # tokens.css（官方藍）/ main.css
```

---

## 6. 啟動與部署

### 本機開發

```bash
cd apps/csp-governance-ui
npm install
npm run dev            # Vite dev server :5173
```

`vite.config.js` 把 `/api`、`/v1`、`/v2` proxy 到 `http://localhost:8000`（CSP 後端）；先確認後端：`curl -sf http://localhost:8000/health`。本 UI 服務於 origin 根路徑 `/`（`vite.config.js` 未設 `base`）。

### 正式部署（隨 CSP 容器出貨）

治理中心**沒有獨立的 compose service**：它由 [`infra/docker/csp.Dockerfile`](../../infra/docker/csp.Dockerfile) 的 `frontend-build` stage（`node:22-alpine` → `npm install` → `npm run build`）建出 `dist/`，複製為 CSP 映像的 `/app/frontend-dist`，再由 **CSP FastAPI 後端**於 origin 根路徑 `/` 供應靜態檔（`services/csp/app/main.py` 掛 `/assets` + SPA fallback）。

因此**改動治理中心 = 重建 `csp` 映像**：

```bash
docker compose -f compose.yaml build csp        # name: anila → infra/compose/platform.yml
docker compose -f compose.yaml up -d csp
# 日常生命週期走 infra/deployment/scripts/deploy-prod.sh；內網更新走 docs/deploy/UPDATE.md
```

### 驗證閘門

```bash
npm test && npm run build
# 後端測試另在 services/csp： cd services/csp && .venv/bin/python -m pytest
```

---

## 7. 相關文件

- 平台現況見 [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md)。
- 後端：[`../../services/csp/README.md`](../../services/csp/README.md)
- 相鄰入口：任務中心 [`../anila-shell/README.md`](../anila-shell/README.md) · 我的知識庫／產出中心 [`../anilalm/README.md`](../anilalm/README.md)
- 平台整體：[`../../README.md`](../../README.md)

