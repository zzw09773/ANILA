# 00. ANILA Product Constitution

> Status: draft v0.1  
> Purpose: 定義 ANILA 新專案不可偏移的產品敘事、入口邊界與防發散規則。  
> Based on: `prod-intranet-card` 既有系統、v0.2 收斂規劃、使用者已確認決策。  
> Owner: ANILA system owner  
> Review: 每次新增一級入口、artifact 類型、Agent 上架規則、GUI Service 註冊模式時必須重審。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. 北極星

ANILA 是中科院內網 AI 工作台：

> **以任務為入口，以個人 / 專案 / 組織知識與專案入口為來源，以受控模型 / Agent / GUI Service 為能力，以 CSP 治理、權限、分類、引用、trace、審計為底座。**

ANILA 不是：

- 一般聊天機器人。
- 多個產品拼成的入口頁。
- Agent marketplace。
- Studio / ANILALM / CSP 的平行品牌集合。
- 讓使用者直接理解 Router、Model endpoint、ingestion、worker 的技術平台。

---

## 2. 唯一產品入口

正式使用者只看到 **ANILA**。

```text
ANILA
├── 任務中心
├── 我的知識庫
├── 產出中心
└── 專案入口
```

治理中心不是一般使用者入口；它是 Admin / Developer / Service Admin 的控制面。

```text
ANILA Admin
└── 治理中心（CSP）
    ├── 身份 / 部門 / 角色
    ├── 模型治理
    ├── Agent Registry
    ├── Service Registry
    ├── 知識治理
    ├── 機敏分類與單向閂鎖
    └── Trace / Audit / Usage
```

### 與 v0.2 的差異

原 v0.2 固定四入口為「任務中心 / 我的知識庫 / 產出中心 / 治理中心」。本版依使用者決策調整：

- **治理中心改為 Admin-facing 控制面，不作為一般使用者的日常入口。**
- **新增「專案入口」作為一般使用者可見入口**，承載其他小組具 GUI 的服務。
- 其他小組服務不可直接變成 ANILA 外的平行產品；必須透過 Service Registry 註冊並由 ANILA Launch Contract 啟動。

---

## 3. 主流程

```text
提出任務
→ 選擇來源或專案入口
→ CSP Policy Check
→ 建立 Task / Source Snapshot / Trace
→ 派發模型、Agent、Studio 或 GUI Service
→ 產出回答 / artifact / service session
→ 寫入 full trace、usage、audit、classification event
```

---

## 4. 產品語彙

| 內部技術名 | 使用者語彙 | 定位 |
|---|---|---|
| `ANILA_UI/anila-ui` | 任務中心 | 使用者任務工作台 |
| `ANILALM` | 我的知識庫 | 個人 / 專案 / 組織知識工作區（個人 / 專案 / 組織分層為目標新增；現況 collection 無 scope 欄位） |
| `anila-studio` | 產出中心 / 產出引擎 | 報告、簡報、心智圖、資訊圖、資料表 |
| `myCSPPlatform` | 治理中心 / CSP | Control Plane + Data Plane |
| `anila-core` | Runtime foundation | Agent / Router / session / tool / memory 基底 |
| `anila-core-router` | Router | OpenAI-compatible 任務分派器 |
| `ingestion-worker` | 知識入庫 worker | Parse / chunk / embed / pgvector |
| `platform_links` / `service_access_grants` | 專案入口 | GUI Service 入口基礎，需升級為 Service Registry |

---

## 5. 功能准入合約

任何新功能必須回答：

| 問題 | 合格答案 |
|---|---|
| 它服務哪個任務？ | 查詢、彙整、分析、比對、草擬、產出、治理、專案服務入口 |
| 它屬於哪個入口？ | 一般使用者四入口：任務中心、我的知識庫、產出中心、專案入口；或 admin-facing 入口：治理中心（僅 Admin / Developer / Service Admin，不與一般使用者入口同層） |
| 它使用哪種來源？ | 無來源、個人、專案、組織、GUI Service 自管資料、系統設定 |
| 它派發哪種能力？ | 模型、Agent、檢索、Studio、GUI Service、治理操作 |
| 它如何 trace？ | 必須產生或繼承 `trace_id`，並能進 Full Trace tree |
| 它如何 audit？ | 必須寫入 actor、action、resource、policy decision、result |
| 它是否改變分類？ | 必須通過 classification propagation |
| 它是否引入新產品敘事？ | 預設不允許；若有必須 ADR |

---

## 6. 凍結清單

v1 前預設不做：

- 新的一級產品品牌。
- 無 CSP 註冊的 Agent。
- 無 full trace 的正式 Agent。
- 無 Service Registry 的 GUI Service 入口。
- Agent marketplace / MCP marketplace。
- 使用者自組 multi-agent swarm。
- 未綁定 Task 的 artifact 產出。
- 未通過 classification policy 的資料匯出。
- 將 OpenWebUI 作為正式入口或 runtime 依賴。
- 讓模型 endpoint API Key 暴露給 Agent 或前端。

---

## 7. 允許例外

以下可進 v1：

- 安全修補。
- SSO / card auth / JWT / revocation / CSRF / RLS 相關可靠性強化。
- 讓既有 flow 更完整 trace / audit 的改動。
- 將既有 OpenWebUI 上的 ML Team Agent 遷移（重新註冊）到 CSP Agent Registry。（✅ 已拍板 2026-07-02：遷移「目標」保留，重新註冊一律走現有精靈 / CLI；自動化匯出匯入工具不列入範圍——見 ADR-0003 註記。）
- 將既有 `platform_links` 擴充成完整 Project Entry / Service Registry 的改動。
- 將 boolean classified latch 升級為多級分類與核准降級流程的改動。

---

## 8. 決策紀錄

### ADR-0001: ANILA 是唯一入口

所有終端使用者任務從 ANILA 進入。CSP 是治理控制面，不是一般使用者的工作入口。

### ADR-0002: Project Entry 是正式入口

其他小組具 GUI 的服務可成為「專案入口」，但必須透過 CSP Service Registry、院內憑證卡 SSO、Launch Token、iframe allow policy、audit callback 註冊。

### ADR-0003: OpenWebUI 只是暫時 Agent 註冊場

OpenWebUI 不作為正式入口，不作為 legacy agent host，不納入 runtime architecture。目標是將 ML Team 目前註冊於 OpenWebUI 的 Agent 轉註冊到 CSP。

> ✅ 已拍板（2026-07-02）：**目標不變**——ML Team 現於 OpenWebUI 上的 Agent 仍要收回、改註冊到 CSP Agent Registry；但**自動化匯出匯入橋接工具不列入範圍**，Agent 一律透過現有註冊精靈 / CLI 手動重新註冊。（此裁決同時調和文件 01 人工複核註記「OpenWebUI 之匯出匯入與本專案開發毫無意義」——該註記針對的是自動化工具，與遷移目標不衝突。）

### ADR-0004: Full Trace 是正式 Agent 的最低要求

正式 Agent 必須回報 run / step / tool / retrieval / model / output / error 的 trace events。只有模型呼叫級 usage 不足以進正式任務。

### ADR-0005: 單向閂鎖是安全 invariant

分類等級為：

```text
無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密
```

上鎖後僅 Admin 可申請降級，且需要主管批核。系統不得自動降級原物件。

---

## 9. Repo evidence / 現況補齊

### Project Entry 現況

目前 repo 已有「專案入口」雛形，但它仍是 link catalog，不是完整 Launch Gateway。

- Backend model：`myCSPPlatform/backend/app/models/platform_link.py` 的 `PlatformLink` 欄位包含 `name`、`url`、`icon`、`description`、`sort_order`、`is_active`、`is_public`、`required_roles`。
- Access model：`service_access_grants` 支援 user-level 或 department-level grant，DB constraint 要求 `user_id` 與 `department_id` 必須二選一；`revoked_at` 作為 soft revoke。
- Access decision：`myCSPPlatform/backend/app/services/access_control.py` 的順序是 active check、admin / owner bypass、role gate、`is_public` bypass、user / department grant。
- API：`GET /api/platform-links` 需要登入；admin / owner 可看全部，其他使用者只拿 `accessible_links_for()` 過濾後結果。`/api/service-access-grants` 是 admin CRUD。
- Frontend：`myCSPPlatform/frontend/src/views/DashboardView.vue` 會取 `listPlatformLinks()`，用 `PlatformCard` 呈現；`PlatformCard.vue` 目前是 `<a target="_blank" rel="noopener">` 外開新分頁。
- Admin UI：`PlatformLinksView.vue` 管 `name/url/icon/description/sort_order/is_public/required_roles`；`ServiceAccessView.vue` 管 user / department grants。

因此 `platform_links` 目前不支援 iframe、launch token、SSO mode、audit callback、healthcheck、owner department / owner admin、classification ceiling，也沒有 service session lifecycle。它可以作為 `RegisteredService` 的 migration seed，但不可視為已完成 Project Entry。

### 入口與導覽現況

- `ANILA_UI/anila-ui/src/main.jsx`（origin 現況）只有 `/app/*` 與 catch-all 導回 `/app`；`/login` 不在本 SPA（原始碼註解明示），未登入由 RequireAuth 導向 CSP 登入。它是 runtime chat UI，尚未有「專案入口」route。
- nginx 已在 443 以同源子路徑服務 ANILA UI：`myCSPPlatform/docker/nginx.conf` 的 `location = /anila`（301 補尾斜線）與 `location /anila/`（反代 `anila-ui:80`）；`docker-compose.yml` 以 build arg `BASE_PATH=${ANILA_UI_BASE_PATH:-/anila/}` 建置 anila-ui。同源即共用 CSP 登入 cookie，SSO 零設定。origin 已含此設定（tip commit `a06c0cb`）。
- `ANILA_UI/anila-ui/src/app.jsx` 已會讀 `/api/banners/active`，在 chat area 上方顯示公告 banner，並用 `localStorage` 記住 dismissed banner。
- `ANILALM/src/App.tsx` 目前是知識庫 SPA，route 為 `/`、`/c/:collectionId`、`/c/:collectionId/conv/:conversationId`；`ProtectedRoute.tsx` 會把未登入者導回 CSP login。原始碼另帶一頁完整的本地帳密登入頁 `src/routes/LoginPage.tsx`（username / password、終端機風格），但在 `origin/prod-intranet-card` 上未掛載 `/login` route（App.tsx 註解明示留檔不掛載，唯一登入頁是 CSP `/login`）；main 系分支則有掛載 `/login`。
- `myCSPPlatform/frontend/src/router/index.js` 已有治理中心 route：`platform-links`、`service-access`、`banners`、`alerts`、`audit-logs`、`service-clients`、`trusted-hosts`、`knowledge-collections` 等。

路由收斂已實際起步：ANILA UI 與 CSP 已同源（443 `/anila/`），後續「唯一入口」整併可在此基礎上進行。最小改動路徑是：在 ANILA Shell / `ANILA_UI` 新增「專案入口」route 或側欄項，呼叫現有 `GET /api/platform-links` 顯示可用服務；治理仍留在 CSP 的 `PlatformLinksView` / `ServiceAccessView`。若 v1 還沒有 Launch Gateway，可先保留外開新分頁，但 contract 必須明確標示 `launch_mode="new_tab"`，避免誤認已支援 iframe。

### `banners`、`alerts`、`platform_links` 不應合併成同一 domain object

- `banners` 是使用者公告：`Banner` 欄位是 `level/content/is_active/sort_order/created_by_user_id/created_at`，`GET /api/banners/active` 供一般登入使用者讀取，ANILA_UI 會呈現在對話介面上方。
- `alerts` 是 admin ops 告警：`Alert` 有 `fingerprint/category/severity/source_type/source_id/title/message/status/metadata_json/first_seen_at/last_seen_at/acknowledged_at/acknowledged_by_user_id/resolved_at`（`models/alert.py`；沒有單一 `source` 欄位，告警來源以 `source_type` + `source_id` 兩欄表達），API 放在 admin 路徑，用於治理中心摘要、ack、resolve。
- `platform_links` 是服務入口與授權清單：它決定使用者可看見哪些外部 GUI service link。

三者可以在治理中心視覺上並列，也可以在 ANILA Shell 首頁共同露出摘要；但 domain 不應合併。Project Entry 應只從 `platform_links` / `service_access_grants` 擴充為 Service Registry / Launch Gateway。`banners` 可用於入口公告，`alerts` 可用於管理者服務健康或安全告警，不應承載 launch authorization。
