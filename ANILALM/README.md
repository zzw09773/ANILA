# ANILA LM (ANILALM)

> 知識庫前端 + Studio 入口：研究筆記風格的 SPA（Vite + React + TS），加上一個獨立的 `pptx-renderer`（pptx-skill）微服務，把投影片規格（deck spec）渲染成 `.pptx`。

> English mirror：[README.en.md](./README.en.md)

> 🌿 **分支對照**：本子專案存在於 `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`。**`trial-military` 精簡版不含本子專案**。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

**ANILALM** 提供「文件 → 對話 → 產出」一站式介面：上傳 → 建知識庫 → 對話查詢 → 生成深度報告與多種 artifact。它本身是 SPA，串接 myCSPPlatform 做認證 / ingestion / 對話 / LLM proxy，artifact 生成則走 [`anila-studio`](../anila-studio/)（slides / reports / mindmaps / infographics / datatables 五種）。mount 在 nginx `/anilalm/` 子路徑。

子專案含兩個獨立執行單元：

1. **頂層 ANILALM app**（package `anilalm` v1.0.0）— Vite + React + TS 前端。
2. **`pptx-skill/`（pptx-renderer 服務）**（package `anilalm-pptx-skill` v0.1.0）— 獨立 Node/Express 服務（`server.js`），用 pptxgenjs + headless LibreOffice + Poppler 把 deck spec 渲染成 `.pptx`。docker network 上以 `pptx-renderer:7100` 對外，**由 anila-studio server-to-server 呼叫，前端不直接打它**。端點：
   - `GET /health` → `ok`
   - `POST /render` — body `{ spec }`，回 `.pptx` 二進位（含 `X-Pptx-Job-Id` / `X-Pptx-Path` header；`spec.slides` 空陣列回 400、超過 `MAX_SLIDES` 回 413）
   - `POST /screenshots` — body `{ pptxPath }` 或 `{ pptxBase64 }`（path 須在 TMP_ROOT 內，防 traversal）；soffice→PDF→pdftoppm(`-r 96`)→PNG，回 `{ images:[{index,mime,base64}] }`
   - `POST /qa-geometric` — body `{ pptxBase64 }`；JSZip 解 + 解析 slide XML，回 `{ defects:[{slide_index,severity,kind,detail}] }`

---

## 架構與技術棧

### 頂層 ANILALM app（前端）

| 模組 | 版本（`package.json`） |
| --- | --- |
| Build | Vite 6.0.5 + React 18.3.1 + TypeScript 5.7.2 |
| 路由 | react-router-dom 6.28.0（`BrowserRouter` + 巢狀 Outlet 守衛） |
| 狀態 | Zustand 5.0.2（auth / workspace / artifacts） |
| HTTP | axios 1.7.9 + 攔截器（401 token refresh、`withCredentials`） |
| Markdown | marked 14.1.3 + DOMPurify 3.2.3（LLM 輸出視為 untrusted，雙層防 XSS） |
| 型別 codegen | openapi-typescript 7.13.0（dev） |
| 圖示 | inline SVG（自製集合，0 套件） |

scripts：`dev`（vite）/ `build`（`tsc -b && vite build`）/ `preview` / `typecheck` / `gen:studio-types`（`bash scripts/gen-studio-types.sh`，對 `../anila-studio/openapi/studio.openapi.json` 跑 `openapi-typescript` 寫 `src/api/studio-types.gen.ts`）。Runtime image：`node:22-alpine` build（`npm install`）→ `nginx:1.27-alpine` 服務 `dist/`；`ARG BASE_PATH=/anilalm/`、`ARG VITE_DEFAULT_CHAT_MODEL=gpt-4o-mini`；`EXPOSE 80`、healthcheck wget `/health`。

### pptx-skill / pptx-renderer 服務

| 相依 | 版本 |
| --- | --- |
| `express` | ^5.2.1 |
| `pptxgenjs` | ^3.12.0 |
| `sharp` | ^0.33.5 |
| `react` / `react-dom` / `react-icons` | ^18.3.1 / ^18.3.1 / ^5.4.0（`icons.js` 概念名→Heroicons PNG） |

> `jszip`（`/qa-geometric` 用）為 `require` 但未列在 `package.json`，靠 lockfile / 傳遞相依解析。

`Dockerfile`：base `node:22-bookworm-slim`（非 alpine），用 **`npm ci --omit=dev`**（不是 vendored COPY）。apt 套件：`libreoffice-core` / `libreoffice-impress`（`.pptx → PDF`）、`poppler-utils`（PDF → PNG）、`fonts-noto-cjk` + **`fonts-noto-cjk-extra`**、`tini`（PID-1 reaper，讓 SIGTERM 傳到 soffice）、`ca-certificates`。ENV `PORT=7100`、`PPTX_TMP_DIR=/var/anila/pptx-out`（`server.js` 程式碼 fallback 為 `/tmp/pptx-out`）。`MAX_PAYLOAD=10mb`、`MAX_SLIDES=60`。`server.js` schema-light（CSP 已做 Pydantic 驗證），只檢查 payload 大小 / 投影片數 / `/screenshots` 路徑。

---

## 目錄結構

```
ANILALM/
├── package.json                # anilalm v1.0.0：react / axios / zustand / marked / dompurify / react-router
├── Dockerfile                  # 前端 image：node:22-alpine（npm install）→ nginx
├── vite.config.ts              # /api、/v1、/v2 → VITE_CSP_BACKEND；/api/studio → VITE_ANILA_STUDIO_BACKEND
├── tsconfig*.json · index.html · docker/(nginx.conf) · _design/ · scripts/gen-studio-types.sh
├── src/
│   ├── main.tsx / App.tsx / types.ts / vite-env.d.ts
│   ├── api/                    # client.ts(axios + STUDIO_BASE_URL) · auth · chat · collections ·
│   │                           #   conversations · documents · jobs · search · studio · studio-types.gen.ts
│   ├── store/                  # auth.ts / workspace.ts / artifacts.ts (Zustand)
│   ├── routes/                 # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
│   ├── workspace/              # WSSidebar / WSChat / WSStudio / CommandModal / ArtifactViewer /
│   │                           #   StudioWizard / ThemePicker / useJobStream
│   ├── studio/                 # generators.ts / themeMapping.ts / themes.ts
│   ├── theme/                  # ThemeContext.tsx / tokens.ts
│   ├── components/             # ErrorBoundary / Field / Icon / MarkdownPreview / Modal / Spinner / ThemeSwitch
│   └── utils/format.ts
└── pptx-skill/                 # ── 獨立的 pptx-renderer 服務 ──
    ├── server.js               # Express：/render /screenshots /qa-geometric /health（port 7100）
    ├── icons.js · package.json（anilalm-pptx-skill v0.1.0）
    ├── Dockerfile              # node:22-bookworm-slim + npm ci + LibreOffice + Poppler + Noto CJK(+extra) + tini
    ├── SKILL.md / pptxgenjs.md / editing.md
    ├── scripts/                # Python helper（add_slide / clean / thumbnail + office/）
    └── tests/                  # 4 檔：test_cover_hero_guard / test_hierarchy_bullets /
                                #   test_image_focus_render / test_local_emptiness
```

---

## 啟動與部署

### 前端（開發模式）

```bash
cd ANILALM
npm install
cp .env.example .env              # 視需要改 VITE_CSP_BACKEND / VITE_ANILA_STUDIO_BACKEND / VITE_DEFAULT_CHAT_MODEL
npm run dev                       # http://localhost:5174
```

dev server 把 `/api`、`/v1`、`/v2` proxy 到 `VITE_CSP_BACKEND`（預設 `http://localhost:8000`），`/api/studio` proxy 到 `VITE_ANILA_STUDIO_BACKEND`（預設 `http://localhost:8100`）。先確認 backend：`curl -sf http://localhost:8000/health`。

### pptx-renderer 服務（容器）

定義於 repo 根 `docker-compose-dev.yml`：`build.context: ANILALM/pptx-skill`、`expose: "7100"`（無 host port，由 anila-studio 以 `pptx-renderer:7100` 連線）、healthcheck `http://127.0.0.1:7100/health`。

```bash
cd <repo_root> && docker compose -f docker-compose-dev.yml up -d pptx-renderer
# 或本機：cd ANILALM/pptx-skill && node server.js   # :7100
```

### smoke 測試

```bash
cd ANILALM/pptx-skill
node tests/test_image_focus_render.js        # 需 live renderer；RENDERER_URL 可覆寫
node tests/test_local_emptiness.js           # inline，不需 server
```

---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `VITE_CSP_BACKEND` | `http://localhost:8000` | dev proxy：`/api`、`/v1`、`/v2`（CSP） |
| `VITE_ANILA_STUDIO_BACKEND` | `http://localhost:8100` | dev proxy：`/api/studio/*`（anila-studio） |
| `VITE_STUDIO_BASE_URL` | `""` | 瀏覽器可見的 anila-studio base（`src/api/client.ts`）；空 = 用 vite proxy / 同源 nginx |
| `VITE_DEFAULT_CHAT_MODEL` | `gpt-4o-mini` | 預設聊天模型（`src/studio/generators.ts` + Dockerfile ARG） |
| `BASE_PATH` | `/anilalm/`（Dockerfile ARG；`.env.example` 註解） | SPA URL 前綴；`vite.config.ts` 讀 `BASE_PATH \|\| VITE_BASE_PATH \|\| '/'` |

---

## 與其他服務的關係

前端只跟 **CSP**（`/api`、`/v1`、`/v2`）與 **anila-studio**（`/api/studio/*` 及 `/api/{reports,mindmaps,infographics,datatables}/*`）溝通；`pptx-renderer` 由 **anila-studio** server-to-server 呼叫（前端 `src/` 無任何 renderer 參考）。前端走 `VITE_STUDIO_BASE_URL`（`src/api/studio.ts` 的 `studioFetch`）。

artifact 皆 async job 模式：`POST /api/{kind}/jobs`（slides 回 202 + JobStatus）→ 輪詢 `GET …/{id}` → `GET …/{id}/download/{fmt}`（slides 為 `/pptx`）→ `DELETE …/{id}` 取消。slides 的 `generateSlides` 另在前端先 `chatComplete` 草擬 JSON spec；report/mindmap/infographic/datatable 則純後端 job。

`image_focus` 渲染（`server.js`）：`image_focus` layout 把 `image_data` 畫左半、bullets 右半；`standard` / `stat_callout` / `quote` / `two_column` / `icon_rows` 忽略 `image_data`。例外：`section_break` 與自動加的封面（`image_gen_meta.use_case==='cover_hero'`）會把 `image_data` 當全幅 hero。

> 重用性：renderer 是獨立 HTTP 服務，未來任何 caller（n8n / CLI / bot）都可打同一個 `/render`，CSP 容器維持 Python-only。

---

## 相關文件

- Studio FLUX 主規格：[`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- 分階段設計 / 計畫：`../docs/superpowers/studio-flux/specs/`、`../docs/superpowers/studio-flux/plans/`
- anila-studio 服務：[`../anila-studio/README.md`](../anila-studio/README.md)
- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
- renderer 內部參考：`pptx-skill/SKILL.md`、`pptx-skill/pptxgenjs.md`
