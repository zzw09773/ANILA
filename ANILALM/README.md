# ANILA LM (ANILALM)

> 知識庫前端 + Studio 入口：研究筆記風格的 SPA，加上一個獨立的 `pptx-renderer` 微服務，把投影片規格（deck spec）轉成 `.pptx`。

> English mirror：[README.en.md](./README.en.md)

> 🌿 **分支對照**：本子專案存在於 `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`。**`trial-military` 精簡版不含本子專案**（移除知識庫管理 SPA 與 Studio 簡報生成）。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 簡介

**ANILALM** 提供面向研究人員的「文件 → 對話 → 產出」一站式介面：上傳 PDF / 文件 → 建知識庫 → 對話查詢 → 生成深度報告與簡報草稿。它本身是 SPA，串接 myCSPPlatform 做認證、ingestion、對話、LLM proxy，簡報生成則走 [`anila-studio`](../anila-studio/)。

子專案內含兩個獨立執行單元：

1. **頂層 ANILALM app** — Vite + React + TypeScript 前端，build 成靜態檔由 nginx 服務，mount 在 nginx `/anilalm/` 子路徑。
2. **`pptx-skill/`（pptx-renderer 服務）** — 獨立 Node 服務（`server.js`），用 Express + pptxgenjs 把 deck spec 渲染成 `.pptx`。docker network 上以 `pptx-renderer:7100` 對外，端點：
   - `POST /render` — 收 `{ spec }`，回 `.pptx` 二進位（octet-stream）。
   - `POST /screenshots` — 收 `.pptx`（base64 或 server 路徑），LibreOffice headless → PDF → Poppler → PNG，回每張投影片影像供 vision QA。
   - `POST /qa-geometric` — 對渲染出的投影片做幾何排版檢查（偵測重疊、最大空白區塊）。
   - `GET /health` — 回 `ok`。

> 前端不直接打 renderer；renderer 由 anila-studio 呼叫。

---

## 架構與技術棧

### 頂層 ANILALM app（前端）

| 模組 | 選擇 |
| --- | --- |
| Build | Vite 6 + React 18 + TypeScript 5.7 |
| 路由 | react-router-dom v6（`BrowserRouter` + 巢狀 Outlet 守衛） |
| 狀態 | Zustand（auth / workspace / artifacts） |
| HTTP | axios + 攔截器（401 token refresh、`withCredentials`） |
| Markdown | marked + DOMPurify（LLM 輸出視為 untrusted，雙層防 XSS） |
| 圖示 | inline SVG（自製集合，0 套件） |

npm scripts：`dev`（vite）、`build`（`tsc -b && vite build`）、`preview`、`typecheck`、`gen:studio-types`。Runtime image（頂層 `Dockerfile`）：multi-stage，`node:22-alpine` build → `nginx:1.27-alpine` 服務 `dist/`；`BASE_PATH` build-arg 預設 `/anilalm/`。

### pptx-skill / pptx-renderer 服務

| 相依 | 用途 |
| --- | --- |
| `express` ^5 | HTTP server |
| `pptxgenjs` ^3.12 | 產生 `.pptx` |
| `sharp` ^0.33 | 影像處理 |
| `react` / `react-dom` / `react-icons` | icon 解析（`icons.js` 把概念名轉成 Heroicons PNG） |

`pptx-skill/Dockerfile` 用 `node:22-bookworm-slim`（非 alpine），額外裝 `libreoffice-core` / `libreoffice-impress`（`.pptx → PDF`）、`poppler-utils`（PDF → PNG）、`fonts-noto-cjk`（CJK 字型）、`tini`（PID-1 reaper，讓 SIGTERM 正確傳到 soffice 子行程）。`node_modules` 為 vendored（`npm ci --omit=dev`，air-gap build）。預設 `PORT=7100`、`PPTX_TMP_DIR=/var/anila/pptx-out`。`server.js` schema-light（CSP 已做 Pydantic 驗證），只檢查 payload 大小（`MAX_PAYLOAD=10mb`）、投影片數（`MAX_SLIDES=60`）、`/screenshots` 路徑（防 traversal）。

---

## 目錄結構

```
ANILALM/
├── package.json                # 前端：react / axios / zustand / marked / dompurify / react-router
├── Dockerfile                  # 前端 image：Vite build → nginx
├── vite.config.ts              # /api、/v1、/v2 proxy 到 VITE_CSP_BACKEND
├── index.html
├── docker/                     # nginx.conf 等部署設定
├── _design/                    # 舊原型（保留作設計參考，不參與 build）
├── src/
│   ├── main.tsx / App.tsx      # createRoot + ThemeProvider + BrowserRouter
│   ├── api/                    # axios client + auth/collections/documents/jobs/conversations/chat/studio
│   ├── store/                  # auth.ts / workspace.ts / artifacts.ts (Zustand)
│   ├── routes/                 # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
│   ├── workspace/              # WSSidebar / WSChat / WSStudio / CommandModal / ArtifactViewer / useJobStream
│   ├── studio/generators.ts    # generateReport / generateSlides
│   ├── theme/ · components/ · utils/format.ts
└── pptx-skill/                 # ── 獨立的 pptx-renderer 服務 ──
    ├── server.js               # Express：/render /screenshots /qa-geometric /health（port 7100）
    ├── icons.js                # 概念名 → Heroicons PNG 解析器
    ├── package.json            # vendored runtime deps
    ├── Dockerfile              # node:22-bookworm-slim + LibreOffice + Poppler + Noto CJK + tini
    ├── SKILL.md / pptxgenjs.md / editing.md
    ├── scripts/
    └── tests/                  # smoke 測試（test_image_focus_render.js / test_local_emptiness.js）
```

---

## 啟動與部署

### 前端（開發模式）

```bash
cd ANILALM
npm install
cp .env.example .env              # 視需要改 VITE_CSP_BACKEND / VITE_DEFAULT_CHAT_MODEL
npm run dev                       # http://localhost:5174
```

dev server 把 `/api`、`/v1`、`/v2` proxy 到 `VITE_CSP_BACKEND`（預設 `http://localhost:8000`）。請先確認 backend 已起：`curl -sf http://localhost:8000/health`。

### pptx-renderer 服務（容器）

`pptx-renderer` 定義於 repo 根 `docker-compose-dev.yml`：`build.context: ANILALM/pptx-skill`、`expose: "7100"`（無 host port，由 anila-studio 以 `pptx-renderer:7100` 連線）、healthcheck `http://127.0.0.1:7100/health`。

```bash
cd <repo_root> && docker compose -f docker-compose-dev.yml up -d pptx-renderer
# 或本機：cd ANILALM/pptx-skill && node server.js   # :7100
```

### smoke 測試

```bash
cd ANILALM/pptx-skill
node server.js &                                   # 或用運行中的容器
node tests/test_image_focus_render.js              # 需 live renderer（走完整 PptxGenJS pipeline）
RENDERER_URL=http://pptx-renderer:7100 node tests/test_image_focus_render.js
node tests/test_local_emptiness.js                 # inline findLargestEmptyRegion，不需 server
```

> `test_local_emptiness.js` 內含一份 `server.js` 函式副本（import server.js 會啟 listener）；改 `server.js` 實作需同步更新副本。

---

## 與其他服務的關係

`pptx-renderer` 不被前端直接呼叫，而是被 **[`anila-studio`](../anila-studio/) service** 呼叫（2026-05-23 從 csp 抽出，PR #12）。前端則走 `VITE_STUDIO_BASE_URL` 指向 anila-studio：

1. anila-studio 收到簡報生成請求後，先由 csp `/api/proxy/v1/chat/completions` 跑 LLM 產出 deck spec（每張投影片有 `title` / `bullets` / `layout_kind` 等）。
2. spec 中標 `image_focus` 的投影片，其 `image_ref` 被 hydrate 成 inline `image_data`（bytes）後才送渲染 — studio FLUX 即時生成的插畫在這步注入；anila-studio 透過 `GET /api/ingestion/images/{id}/blob` 取原始 image bytes。
3. anila-studio `POST {RENDERER_BASE_URL}/render` 帶 `{ spec }`，拿回 `.pptx` bytes。
4. 後續 vision / 幾何 QA 再呼叫 `POST /screenshots`（拿 PNG）與 `POST /qa-geometric`。

`image_focus` 渲染行為（由 `test_image_focus_render.js` 守護）：只有 `image_focus` layout 會畫 `image_data`；`standard` / `stat_callout` / `quote` / `two_column` / `icon_rows` 都忽略。

> 重用性：renderer 是獨立 HTTP 服務，任何未來 caller（n8n node、CLI、bot）都可打同一個 `/render`，CSP 容器維持 Python-only、不需內嵌 Node + LibreOffice。

---

## 相關文件

- Studio FLUX 主規格：[`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)（多階段合約、元件盤點）
- 分階段設計 / 計畫：`../docs/superpowers/studio-flux/specs/`、`../docs/superpowers/studio-flux/plans/`
- anila-studio 服務：[`../anila-studio/README.md`](../anila-studio/README.md)
- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
- renderer 內部參考：`pptx-skill/SKILL.md`、`pptx-skill/pptxgenjs.md`
