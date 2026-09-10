# ANILA LM — 我的知識庫 / 產出中心（`anilalm`）

> ANILA 的知識與產出 SPA（Vite + React + TypeScript，v1.0.0）。承載「**我的知識庫**」（collection／文件／檢索／對話）與「**產出中心**」（Studio：簡報 / 報告 / 心智圖 / 資訊圖 / 資料表五種 artifact）。掛載於 nginx 同源子路徑 `/anilalm/`，由 ANILA Shell 的四入口導覽進入。

> English mirror: [`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本子專案存在於 `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`；**`trial-military` 精簡版不含本子專案**。分支策略見根目錄 [`README.md`](../../README.md) （現行單一 `main`；舊七分支模型已失效，見根目錄 README）。
>
> 設計沿革（收斂紀錄）：[`docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)（憲章）、[`01-domain-model.md`](../../docs/anila-redesign-docs/01-domain-model.md)（Task 網域）、[`09-api-event-contracts.md`](../../docs/anila-redesign-docs/09-api-event-contracts.md)（Task / artifact 契約）。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。

---

## 1. 產品定位

本 SPA 提供「文件 → 對話 → 產出」一站式流程：上傳建知識庫 → 對話查詢 → 生成 artifact。它**只是前端**，串接 [`services/csp`](../../services/csp/)（CSP）做認證 / ingestion / 對話 / LLM proxy，artifact 生成走 [`services/anila-studio`](../../services/anila-studio/)。

在 ANILA 產品憲章（doc 00 §2）中，本 SPA 同時實作兩個一級使用者入口 —「我的知識庫」與「產出中心」；ANILA Shell 的側欄以同源絕對路徑 `/anilalm` 進入。

---

## 2. Task-first 產出（redesign Slice 8b）

**每一次 Studio 產出 job 送出前，先在 CSP 建立一個 Task**，讓後續的 artifact-job / artifact / trace 全部掛回同一個治理單元（doc 09 §2 Task API）。

- `src/api/tasks.ts` — `createArtifactTask()` → `POST /api/tasks`（`task_type:'generate_artifact'`、`source_scope` 預設 `'project'`、`selected_collection_ids`、`requested_output_type`）。回傳 `TaskBinding { taskId, sourceSnapshotId?, traceId? }`，再 thread 進 Studio job body。型別對映 `services/csp/app/schemas/contracts/tasks.py` 與 doc 01（`TaskType` / `SourceScope` / `RequestedOutputType`）。
- **韌性契約**：任何失敗（端點未上線 / 認證 / 網路）回傳 `null` ＋ zh-TW `console.warn`，產出**以無任務綁定方式照常繼續**，絕不因治理 metadata 掛不上而中斷生成。
- 呼叫點：`src/workspace/CommandModal.tsx`（簡報）與 `src/studio/generators.ts`（報告 / 心智圖 / 資訊圖 / 資料表）——五種 artifact 皆先建 Task。

### Artifact 皆 async job 模式

| Artifact | 送出 | 輪詢 / 下載 |
|---|---|---|
| 簡報 slides | `POST /api/studio/slides/jobs` → 202 `JobStatus` | `GET …/{id}` 輪詢 → `GET …/{id}/pptx` 下載 → `DELETE …/{id}` 取消（`src/api/studio.ts`，走 `STUDIO_BASE_URL`） |
| 報告 report | `POST /api/reports/jobs` | 前端先 `chatComplete` 草擬 JSON spec，再建 job（`src/studio/generators.ts`） |
| 心智圖 / 資訊圖 / 資料表 | `POST /api/{kind}/jobs` | 同上 job 模式 |

`src/workspace/WSStudio.tsx` 的輪詢 effect 依 artifact `state === 'pending'` 驅動 done / failed 轉換與下載；使用者可在生成期間繼續操作。

---

## 3. 技術棧（讀自 `package.json`）

| 模組 | 版本 |
| --- | --- |
| Build | **Vite 6.0.5 + React 18.3.1 + TypeScript 5.7.2** |
| 路由 | `react-router` 7.18.2（`>=7.18.2 <8`；`BrowserRouter` + 巢狀 Outlet 守衛） |
| 狀態 | **Zustand 5.0.2**（auth / workspace / artifacts） |
| HTTP | `axios` 1.7.9 + 攔截器（401 refresh、`withCredentials`） |
| Markdown | `marked` 14.1.3 + `DOMPurify` 3.2.3（LLM 輸出視為 untrusted，雙層防 XSS） |
| 型別 codegen | `openapi-typescript` 7.13.0（dev） |
| 圖示 | inline SVG（自製，0 套件） |

`scripts`：`dev` / `build`（**`tsc -b && vite build`**）/ `preview` / `typecheck`（`tsc -b --noEmit`）/ `gen:studio-types`。**驗證閘門 = `typecheck` + `build`**（本子專案無單元測試框架）。

### `gen:studio-types` 流程

`bash scripts/gen-studio-types.sh` 對 `services/anila-studio/openapi/studio.openapi.json` 跑 `openapi-typescript`，寫出 `src/api/studio-types.gen.ts`。anila-studio schema 變動後重跑；產出的型別讓 studio job 契約在 build 時就抓到 drift。

---

## 4. 目錄結構

```
apps/anilalm/
├── package.json · Dockerfile（node:22-alpine build → nginx；ARG BASE_PATH=/anilalm/）
├── vite.config.ts（/api、/v1、/v2 → VITE_CSP_BACKEND；/api/studio → VITE_ANILA_STUDIO_BACKEND）
├── tsconfig*.json · index.html · .env.example · docker/ · _design/ · scripts/gen-studio-types.sh
└── src/
    ├── main.tsx / App.tsx / types.ts / vite-env.d.ts
    ├── api/          # client.ts(axios + STUDIO_BASE_URL) · auth · chat · collections · conversations ·
    │                 #   documents · jobs · search · studio · studio-types.gen.ts · tasks.ts
    ├── store/        # auth.ts / workspace.ts / artifacts.ts（Zustand）
    ├── routes/       # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
    ├── workspace/    # WSSidebar / WSChat / WSStudio / CommandModal / StudioWizard / ArtifactViewer /
    │                 #   ThemePicker / useJobStream
    ├── studio/       # generators.ts（5 種 artifact，皆先建 Task）/ themeMapping.ts / themes.ts
    ├── theme/        # ThemeContext.tsx / tokens.ts
    ├── components/   # ErrorBoundary / Field / Icon / MarkdownPreview / Modal / Spinner / ThemeSwitch
    └── utils/format.ts
```

> **`pptx-renderer` 不在本子專案內**：投影片渲染服務已獨立為 [`services/pptx-renderer`](../../services/pptx-renderer/)，由 **anila-studio** server-to-server 呼叫（`pptx-renderer:7100`），前端不直接打它。細節見該服務自己的 README / `SKILL.md`。

---

## 5. 啟動與部署

### 本機開發

```bash
cd apps/anilalm
npm install
cp .env.example .env               # 視需要改 VITE_CSP_BACKEND / VITE_ANILA_STUDIO_BACKEND / VITE_DEFAULT_CHAT_MODEL
npm run dev                        # http://localhost:5174
```

dev server 把 `/api`、`/v1`、`/v2` proxy 到 `VITE_CSP_BACKEND`（預設 `http://localhost:8000`），`/api/studio` proxy 到 `VITE_ANILA_STUDIO_BACKEND`（預設 `http://localhost:8100`）。先確認後端：`curl -sf http://localhost:8000/health`。

### 容器（monorepo compose）

本 SPA 在 compose 中的 service 名為 **`anilalm`**（build context `apps/anilalm`、`BASE_PATH=/anilalm/`），由根目錄 shim `compose.yaml`（`name: anila-platform`）→ `infra/compose/platform.yml` 納管；dev 為 `compose.dev.yaml` → `infra/compose/dev.yml`。經主 nginx 於同源 `/anilalm/` 反向代理。

```bash
docker compose -f compose.yaml up -d anilalm
# 日常生命週期走 infra/deployment/scripts/deploy-prod.sh
```

### 驗證閘門

```bash
npm run typecheck      # tsc -b --noEmit
npm run build          # tsc -b && vite build（正式驗證用；非只 tsc）
```

---

## 6. 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `VITE_CSP_BACKEND` | `http://localhost:8000` | dev proxy：`/api`、`/v1`、`/v2`（CSP） |
| `VITE_ANILA_STUDIO_BACKEND` | `http://localhost:8100` | dev proxy：`/api/studio/*`（anila-studio） |
| `VITE_STUDIO_BASE_URL` | `""` | 瀏覽器可見的 anila-studio base（`src/api/client.ts`）；空 = 用 vite proxy / 同源 nginx |
| `VITE_DEFAULT_CHAT_MODEL` | `gpt-4o-mini` | 預設聊天模型（須存在於 CSP model_registry 或為已核准 Agent 名） |
| `BASE_PATH` | `/anilalm/`（Dockerfile ARG） | SPA URL 前綴；`vite.config.ts` 讀 `BASE_PATH \|\| VITE_BASE_PATH \|\| '/'` |

---

## 7. 相關文件

- 設計沿革（收斂紀錄）：[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)（憲章 00 / 網域 01 / 契約 09）。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。
- 後端服務：CSP [`../../services/csp/README.md`](../../services/csp/README.md) · Studio [`../../services/anila-studio/README.md`](../../services/anila-studio/README.md) · Renderer [`../../services/pptx-renderer/`](../../services/pptx-renderer/)
- 相鄰入口：任務中心 [`../anila-shell/README.md`](../anila-shell/README.md) · 治理中心 [`../csp-governance-ui/README.md`](../csp-governance-ui/README.md)
- 平台整體：[`../../README.md`](../../README.md) · 現行 `main`（舊七分支模型已失效）

---

**Framework**：Vite + React + TypeScript · **Serves**：我的知識庫 + 產出中心 · **Talks to**：CSP（`/api`、`/v1`、`/v2`）+ anila-studio（`/api/studio/*`、`/api/{reports,mindmaps,infographics,datatables}/*`），皆先建 CSP Task。
