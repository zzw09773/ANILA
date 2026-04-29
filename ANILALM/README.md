# ANILA LM

研究筆記風格的知識庫前端，串接 myCSPPlatform 後端做認證、ingestion、對話與 LLM proxy。

## 為何這個專案存在

`/home/aia/c1147259/ANILA` 已經有一整套後端：myCSPPlatform 提供 auth + ingestion +
conversations + OpenAI 相容 `/v1` proxy；anila-core 提供 chunking / embedding；
ingestion-worker 跑非同步索引。但缺一個面向研究人員、能夠把「文件 → 對話 → 產出」一站
打通的整合介面。

ANILA LM 就是這層皮——上傳 PDF / 文件 → 建知識庫 → 對話查詢 → 直接生成深度報告 / 簡報草稿。

## 技術選擇

| 模組 | 選擇 | 理由 |
| --- | --- | --- |
| Build | Vite 6 + React 18 + TypeScript | 跟 `myCSPPlatform/frontend`（Vue + Vite）並列，避免 CDN-React 原型走不下去 |
| 路由 | react-router-dom v6 | `BrowserRouter` + 巢狀 Outlet 守衛 |
| 狀態 | Zustand（auth / workspace / artifacts） | Pinia 是 Vue 專用，Zustand 等價且 React 友善 |
| HTTP | axios + 攔截器 | 跟 CSP 前端相同 pattern，token refresh on 401 |
| Markdown | marked + DOMPurify | LLM 輸出仍是 untrusted，雙層防 XSS |
| 圖示 | inline SVG（自製集合） | 0 套件、Tree-shake 友善 |

## 後端依賴

ANILALM 直接打 myCSPPlatform 的 endpoint：

| Endpoint | 用途 |
| --- | --- |
| `POST /api/auth/login`、`/refresh`、`/me`、`/logout`、`/register` | 帳號流程 |
| `GET/POST/PATCH/DELETE /api/ingestion/collections[/:id]` | 知識庫 CRUD |
| `GET/POST /api/ingestion/collections/:id/documents` | 文件上傳與列表 |
| `GET /api/ingestion/jobs/:id/stream` | 上傳後的索引進度（SSE） |
| `GET /api/conversations`, `POST /api/conversations/:id/messages` | 對話與訊息持久化 |
| `POST /v1/chat/completions` | LLM 對話與 Studio 生成（OpenAI 相容） |

JWT 同時對 `/api` 與 `/v1` 都通——因為 CSP 後端的 `get_caller` middleware 把
JWT bearer 跟 `sk-*` API key 統一處理；前端不需要再額外請使用者管理 API key。

## 資料夾結構

```
ANILALM/
├── _design/                         # 舊原型（Figma-canvas + 1929-line HTML）保留作設計參考
├── index.html                       # Vite 入口
├── package.json                     # 直接依賴：react / axios / zustand / marked / dompurify / react-router
├── vite.config.ts                   # /api、/v1、/v2 proxy 到 VITE_CSP_BACKEND
├── src/
│   ├── main.tsx                     # createRoot + 副作用 import store/auth（綁定攔截器）
│   ├── App.tsx                      # ThemeProvider + BrowserRouter + 路由表
│   ├── types.ts                     # 後端 schema 的 TS 對應
│   ├── theme/
│   │   ├── tokens.ts                # 雙主題色 token
│   │   └── ThemeContext.tsx         # provider + localStorage 持久化
│   ├── components/                  # Icon / ThemeSwitch / Field / Modal / Spinner / MarkdownPreview / EmptyState / Cite
│   ├── api/                         # axios client + auth/collections/documents/jobs/conversations/chat
│   ├── store/
│   │   ├── auth.ts                  # JWT 持久化 + 自動 401 刷新
│   │   ├── workspace.ts             # 當前知識庫 / docs / conversations
│   │   └── artifacts.ts             # Studio 產出（localStorage 持久化，MVP）
│   ├── routes/
│   │   ├── ProtectedRoute.tsx       # 未登入 → /login，附帶 fromPath
│   │   ├── LoginPage.tsx
│   │   ├── DashboardPage.tsx        # /api/ingestion/collections + 釘選（前端）
│   │   └── WorkspacePage.tsx        # 載入 collection + docs + conversations
│   ├── workspace/
│   │   ├── WSSidebar.tsx            # 文件上傳、對話列表
│   │   ├── WSChat.tsx               # /v1/chat/completions 串流 + /api/conversations 持久化
│   │   ├── WSStudio.tsx             # 製作台 + 已完成 timeline
│   │   ├── CommandModal.tsx         # 2 步驟風格選擇與生成
│   │   ├── ArtifactViewer.tsx       # Markdown 報告 / 投影片瀏覽
│   │   └── useJobStream.ts          # 自動訂閱所有 in-flight job 的 SSE
│   ├── studio/
│   │   └── generators.ts            # generateReport / generateSlides（呼叫 /v1/chat/completions）
│   └── utils/format.ts              # bytes / 相對時間 / 根據 id 配色
└── tsconfig*.json
```

## 開發

```bash
cd /home/aia/c1147259/ANILA/ANILALM
npm install                       # 已執行；node_modules 已就緒
cp .env.example .env              # 視需要改 VITE_CSP_BACKEND / VITE_DEFAULT_CHAT_MODEL
npm run dev                       # http://localhost:5174
```

預設 dev server 會把 `/api`、`/v1`、`/v2` proxy 到 `http://localhost:8000`
（也就是 myCSPPlatform 的 backend）。請先確認 backend 已起：

```bash
cd /home/aia/c1147259/ANILA/myCSPPlatform/backend
docker compose up -d              # 或對應的啟動方式
curl -sf http://localhost:8000/health
```

## 重要設計決策

### 1. JWT 同時走 `/api` 跟 `/v1`
不需要前端再產生 API Key。axios 攔截器把 access_token 當作 Bearer 加上去，CSP
後端的 `get_caller` middleware 會自動辨識為 JWT。

### 2. SSE Job Progress 仰賴 cookie session
`EventSource` 不能設 Authorization header，所以 SSE 倚賴 `_finalize_login` 在登入時
種下的 cookie。axios 也是 `withCredentials: true`，所以 cookie 一直在 scope 內。

### 3. RAG 還沒接，目前是「檔名 + 通用知識」模式
CSP 後端目前沒有 `/api/ingestion/search`（語義搜尋）endpoint。Chat 的 system prompt 會
列出已索引文件的檔名 + 段數，明確告訴模型「片段內容尚未注入」，要回答時就用通用知識
回答並提醒使用者貼段落進對話。等後端有 search endpoint 之後改 `WSChat.buildSystemPrompt`
與 `studio/generators.ts` 的 `summariseSources` 即可升級為真 RAG。

### 4. Studio 9 種輸出，MVP 只開「深度報告」與「簡報」
其餘 7 種（podcast / video / mindmap / flashcards / quiz / infographic / datatable）在
`WSStudio` 顯示但點擊會跳出 "Coming soon" 提示。後端對應路徑（TTS / 影片合成等）就緒後
解鎖即可——`CommandModal` 已預留 2 步驟流程。

### 5. Pinned 與 Studio artifacts 都先存 localStorage
釘選（pin）與 Studio 產出目前都是 client-side。後端如果之後新增
`user_collection_preferences` 與 `studio_artifacts` 兩張表，把
`store/artifacts.ts` / `DashboardPage.tsx` 的 localStorage 替換成 axios 即可，零 UI 改動。

## 已知限制 / 後續

- 沒有 RAG 真檢索；對話會明確告知模型片段未注入。
- Studio 產出存 localStorage；換瀏覽器/裝置看不到歷史。
- 沒有檔案分享 / 多人共用知識庫（CSP 的 `collection_access_grants` 還沒實作）。
- 沒有 zip 批次上傳 UI（後端 `/documents/zip` endpoint 已有，但 MVP 沒接）。
- 沒有 doc preview（後端 `/documents/:id/blob` 可下載，後續可加 PDF preview iframe）。
- 沒有 conversation 分類 / 機密標記 UI（後端 `/classify` endpoint 已有）。
- 沒有 e2e 測試；smoke 測試僅靠 `npm run build`。

## 與舊原型的關係

`_design/` 目錄保留了一份 1929 行的 single-file HTML prototype（`prototype.html`）
與 5 個 Figma-canvas 用的 artboard JSX。它們是這次 redesign 的設計依據，但不再
參與 build——所有 production 邏輯都在 `src/`。
