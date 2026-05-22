# ANILA LM (ANILALM)

> AI 學習內容生成子專案：研究筆記風格的知識庫前端，加上一個獨立的 `pptx-renderer` 微服務，把投影片規格（deck spec）轉成 `.pptx`。

## 簡介 / Overview

**ANILALM** 是 `<project_root>` 底下的子專案，提供面向研究人員的「文件 → 對話 → 產出」一站式介面：上傳 PDF / 文件 → 建知識庫 → 對話查詢 → 直接生成深度報告與簡報草稿。它本身是一支 SPA（單頁應用），串接 myCSPPlatform 後端做認證、ingestion、對話與 LLM proxy。

子專案內含兩個獨立的執行單元：

1. **頂層 ANILALM app** — Vite + React + TypeScript 的前端，build 成靜態檔由 nginx 服務。
2. **`pptx-skill/`（pptx-renderer 服務）** — 一支獨立的 Node 服務（`server.js`），用 Express + pptxgenjs 把 deck spec 渲染成 `.pptx`。它在 docker network 上以 `pptx-renderer` 服務名、port `7100` 對外，提供三個端點：
   - `POST /render` — 收 `{ spec }`，回傳 `.pptx` 二進位（octet-stream）。
   - `POST /screenshots` — 收 `.pptx`（base64 或 server 端路徑），用 LibreOffice headless 轉 PDF、再用 Poppler 轉 PNG，回傳每張投影片的影像，供 vision QA 使用。
   - `POST /qa-geometric` — 對渲染出的投影片做幾何排版檢查（偵測重疊、最大空白區塊等）。
   - `GET /health` — 回 `ok`。

> 兩者目的不同：前端是使用者介面，`pptx-renderer` 是被後端呼叫的渲染引擎；前端本身不直接打 renderer。

## 架構與技術棧 / Architecture & Stack

### 頂層 ANILALM app（前端）

來自 `package.json`：

| 模組 | 選擇 |
| --- | --- |
| Build | Vite 6 + React 18 + TypeScript 5.7 |
| 路由 | react-router-dom v6（`BrowserRouter` + 巢狀 Outlet 守衛） |
| 狀態 | Zustand（auth / workspace / artifacts） |
| HTTP | axios + 攔截器（401 token refresh、`withCredentials`） |
| Markdown | marked + DOMPurify（LLM 輸出視為 untrusted，雙層防 XSS） |
| 圖示 | inline SVG（自製集合，0 套件） |

npm scripts：`dev`（vite）、`build`（`tsc -b && vite build`）、`preview`、`typecheck`。

Runtime image（頂層 `Dockerfile`）：multi-stage，`node:22-alpine` build → `nginx:1.27-alpine` 服務 `dist/`。`BASE_PATH` build-arg 預設 `/anilalm/`，對應 ANILA reverse proxy 後的部署路徑。

### pptx-skill / pptx-renderer 服務

來自 `pptx-skill/package.json`：

| 相依 | 用途 |
| --- | --- |
| `express` ^5 | HTTP server |
| `pptxgenjs` ^3.12 | 產生 `.pptx` |
| `sharp` ^0.33 | 影像處理 |
| `react` / `react-dom` / `react-icons` | icon 解析（`icons.js` 把概念名轉成 Heroicons PNG） |

`pptx-skill/Dockerfile` 用 `node:22-bookworm-slim`（非 alpine），額外裝 `libreoffice-core` / `libreoffice-impress`（`.pptx → PDF`）、`poppler-utils`（PDF → PNG）、`fonts-noto-cjk`（CJK 字型，避免中文變成方框）、`tini`（PID-1 reaper，讓 SIGTERM 正確傳到 soffice 子行程）。`node_modules` 是 vendored（`npm ci --omit=dev`），為了 air-gap build。預設 `PORT=7100`、`PPTX_TMP_DIR=/var/anila/pptx-out`。

`server.js` 設計上 schema-light：CSP 後端會先做 Pydantic 驗證，spec 進到 renderer 時已結構合法；renderer 只檢查 payload 大小（`MAX_PAYLOAD=10mb`）、投影片數上限（`MAX_SLIDES=60`）、以及 `/screenshots` 的路徑（防 traversal）。

## 目錄結構 / Layout

```
ANILALM/
├── package.json                # 前端：react / axios / zustand / marked / dompurify / react-router
├── Dockerfile                  # 前端 image：Vite build → nginx
├── vite.config.ts              # /api、/v1、/v2 proxy 到 VITE_CSP_BACKEND
├── index.html                  # Vite 入口
├── docker/                     # nginx.conf 等部署設定
├── _design/                    # 舊原型（single-file HTML + Figma artboard）保留作設計參考，不參與 build
├── src/
│   ├── main.tsx / App.tsx      # createRoot + ThemeProvider + BrowserRouter
│   ├── api/                    # axios client + auth/collections/documents/jobs/conversations/chat
│   ├── store/                  # auth.ts / workspace.ts / artifacts.ts (Zustand)
│   ├── routes/                 # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
│   ├── workspace/              # WSSidebar / WSChat / WSStudio / CommandModal / ArtifactViewer / useJobStream
│   ├── studio/generators.ts    # generateReport / generateSlides（呼叫 /v1/chat/completions）
│   ├── theme/                  # tokens.ts + ThemeContext.tsx
│   ├── components/             # Icon / ThemeSwitch / Field / Modal / MarkdownPreview ...
│   └── utils/format.ts
└── pptx-skill/                 # ── 獨立的 pptx-renderer 服務 ──
    ├── server.js               # Express app：/render /screenshots /qa-geometric /health（port 7100）
    ├── icons.js                # 概念名 → Heroicons PNG 解析器（server.js 啟動時必需）
    ├── package.json            # vendored runtime deps（express / pptxgenjs / sharp / react-icons）
    ├── Dockerfile              # node:22-bookworm-slim + LibreOffice + Poppler + Noto CJK + tini
    ├── SKILL.md / pptxgenjs.md / editing.md   # skill 文件與 pptxgenjs 參考
    ├── scripts/                # 操作員可在容器內執行的 helper 腳本
    └── tests/                  # smoke 測試
        ├── test_image_focus_render.js   # 對 live renderer 驗證 image_focus 會嵌圖、standard 不會
        └── test_local_emptiness.js      # inline 驗證 findLargestEmptyRegion 的空白區塊判定
```

## 啟動與部署 / Setup & Run

### 前端（開發模式）

```bash
cd <project_root>/ANILALM
npm install                       # node_modules 已就緒
cp .env.example .env              # 視需要改 VITE_CSP_BACKEND / VITE_DEFAULT_CHAT_MODEL
npm run dev                       # http://localhost:5174
```

dev server 會把 `/api`、`/v1`、`/v2` proxy 到 `VITE_CSP_BACKEND`（預設 `http://localhost:8000`，即 myCSPPlatform backend）。請先確認 backend 已起：

```bash
curl -sf http://localhost:8000/health
```

### pptx-renderer 服務（容器）

`pptx-renderer` 在 repo 根的 `docker-compose-dev.yml` 中定義為一個 service：

- `build.context: ANILALM/pptx-skill`
- `expose: "7100"` — 只在 docker network 內（無 host port mapping），由 CSP 後端以服務名 `pptx-renderer:7100` 連線。
- healthcheck 打 `http://127.0.0.1:7100/health`。

從 repo 根啟動：

```bash
cd <project_root>
docker compose -f docker-compose-dev.yml up -d pptx-renderer
```

本機跑（不經 compose）：

```bash
cd <project_root>/ANILALM/pptx-skill
node server.js                    # listening on :7100
```

### 跑 smoke 測試

`test_image_focus_render.js` 需要一個正在運行的 renderer（它走完整 PptxGenJS pipeline，不能 inline）。預設打 `http://localhost:7100`，可用 `RENDERER_URL` 覆寫：

```bash
cd <project_root>/ANILALM/pptx-skill
node server.js &                                   # 或用運行中的容器
node tests/test_image_focus_render.js
RENDERER_URL=http://pptx-renderer:7100 node tests/test_image_focus_render.js
```

`test_local_emptiness.js` 把 `findLargestEmptyRegion` inline 一份，不需 server：

```bash
node tests/test_local_emptiness.js
```

> 注意：`test_local_emptiness.js` 內含一份 `server.js` 函式的副本（因為 `server.js` 一被 import 就會啟 listener）。若 `server.js` 的實作改了，需同步更新這份副本。

## 與其他服務的關係 / Integration

`pptx-renderer` 不被前端直接呼叫，而是被 **myCSPPlatform backend 的 studio 模組** 呼叫（`myCSPPlatform/backend/app/api/studio.py`，常數 `RENDERER_BASE_URL = "http://pptx-renderer:7100"`）：

1. CSP 收到 Studio 生成簡報的請求後，先由 LLM 產出 deck spec（每張投影片有 `title` / `bullets` / `layout_kind` 等）。
2. spec 中標記為 `image_focus` 的投影片，其 `image_ref` 會被 hydrate 成 inline `image_data`（bytes）後才送渲染——studio FLUX 即時生成的情境插畫，就是在這一步被注入 spec。
3. CSP `POST {RENDERER_BASE_URL}/render` 帶 `{ spec }`，拿回 `.pptx` bytes。
4. 後續若要做 vision / 幾何 QA，CSP 再呼叫 `POST /screenshots`（拿 PNG）與 `POST /qa-geometric`。

關於 `image_focus` 的渲染行為（由 `test_image_focus_render.js` 守護）：只有 `image_focus` layout 會把 `image_data` 畫上去；`standard` / `stat_callout` / `quote` / `two_column` / `icon_rows` 都會忽略 `image_data`。

> 重用性：因為 renderer 是獨立 HTTP 服務，任何未來的 caller（n8n workflow node、CLI、bot 等）都可以打同一個 `/render` 端點，而 CSP 容器維持 Python-only、不需內嵌 Node + LibreOffice。

## 相關文件 / Related docs

studio FLUX 圖像生成的合約與分階段規格（路徑相對於本檔）：

- [`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md) — Studio FLUX 主規格（多階段合約、元件盤點）。
- `../docs/superpowers/studio-flux/specs/` — 分階段設計文件：
  - `2026-05-21-stage2-clip-descope-vlm-ranking-design.md`
  - `2026-05-21-stage3-brand-yaml-design.md`
  - `2026-05-21-stage3-content-inferred-style-design.md`
  - `2026-05-21-stage4-illustration-routing-design.md`
- `../docs/superpowers/studio-flux/plans/` — 分階段實作計畫：
  - `2026-05-21-stage2-clip-descope-vlm-ranking.md`
  - `2026-05-21-stage3-content-inferred-style.md`
  - `2026-05-21-stage4-illustration-routing.md`

另見 `pptx-skill/SKILL.md` 與 `pptx-skill/pptxgenjs.md`（renderer 內部的 skill 與 pptxgenjs 參考）。

---

> English mirror: [README.en.md](./README.en.md)
