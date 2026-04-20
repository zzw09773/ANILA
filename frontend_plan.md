# ANILA_UI 視覺重新設計 — 對齊 Feature Showcase PDF

## 背景

Codex 把 `ANILA_UI/ANILA_templete/anila-ui`（Babel-CDN 原型，100% 以 `style={{...}}` inline + `:root` token 系統上色）改寫成 `ANILA_UI/anila-ui`（Vite + React app）時，視覺方向跟 `ANILA — Feature Showcase (Print).pdf`（四頁設計稿）偏離很多。使用者透過比對截圖確認：偏的不只是配色，「**整體版面**」都要重做。

本專案為**企業內網、本地部署** LLM/RAG runtime client，沒有 SaaS 的配額/計費概念，一切稽核留在 CSP proxy。這會影響 login 頁的狀態卡片設計（見 Phase 4）。

**兩個具體偏移：**

1. **色票 / 材質 / 幾何** — Codex 交出來是暖米色 SaaS-luxury 風（`#f4f1eb` 底、圓角 10/16/24、`0 20px 50px` 厚陰影、`rgba(255,255,255,0.88)` 半透明 surface、寫死的 radial-gradient body 底）。PDF 要的是編輯/印刷風：oklch 近白 `oklch(0.985 0.002 90)`、teal accent `#0b7285`、圓角 4/6/10、薄邊框、無裝飾陰影、等寬字標籤（`WAVE 1`、`PAGE 02 · ENTRY`、`trace: t-9f3a2b91`）。

2. **Codex 在 chat view 塞了 PDF 沒有的 UI 家具** — `PAGE 03 · RUNTIME` 橫幅、chat empty state 裡的 `WAVE 1 / WAVE 2 / WAVE 3` 特徵卡片條、assistant 訊息上方堆疊的 `TRACE / SOURCES / CONFIDENCE` 摘要盒、頂端紅色 sticky `機密對話已啟用` 橫幅。PDF page 3 這些東西全部沒有——routing trace 是訊息卡片內的 inline collapsible 條、來源引用是 inline `[1][2][3]` 上標、機密狀態只是 API key pill 旁邊一個小徽章。

**探索階段發現的既有破口**：`components.jsx:18-25` 已經在引用 templete 的 token 名（`--fg`、`--border`、`--bg-elev`、`--bg-subtle`、`--accent-fg`），但這些 **在現在的 `styles.css` 裡根本沒宣告**（現在用的是 `--ink`、`--line`、`--surface`、`--bg-soft` 等舊命名）。所以 `components.jsx`、`trust.jsx`、`tweaks.jsx` 裡的很多 inline style 其實正在 fallback 到瀏覽器預設。這次重設計實際上是**把 Codex 半途停下的 templete token 遷移做完**，而不是從頭換皮。

**現在的 `styles.css` 也沒有任何 `[data-theme="dark"]` 規則** — `tweaks.jsx` 的深色模式切換目前完全不會產生視覺變化。深色調色盤是要新寫，不是移植。

**預期結果**：anila-ui 視覺對齊 PDF 四頁（品牌 header、登入、runtime chat、citations drawer）以及使用者提供的乾淨版目標截圖；同時完整保留 runtime SDK wiring、串流邏輯、auth flow、state machines、fake-data 固件。

---

## 策略

**採 Option B（token 替換 + 針對性 JSX 精簡），不採 Option A（全面改成 inline styles）。**
- 約 259 個 CSS class、2179 行——遷移到 inline styles 像 templete 那樣是重寫不是重皮，而且會牽動 runtime SDK。CSS class 架構保留。
- 接受現有的混合寫法：`components.jsx` / `trust.jsx` 已經是 inline-style-with-CSS-vars（templete 風）混合 `styles.css` 的 class。不要硬去「統一」，只會讓範圍爆炸。讓 class rule 管 layout / pseudo-state（`:hover`、`:focus-visible`、`:disabled`），讓 inline style 管基本幾何。
- **整組採用 templete 的 token 命名**（`--fg`、`--bg`、`--bg-elev`、`--bg-subtle`、`--border`、`--border-strong`、`--fg-muted`、`--fg-subtle`、`--accent`、`--accent-fg`、`--accent-soft`），讓 `components.jsx` 不再引用不存在的變數。遷移期間保留舊名（`--ink`、`--line` 等）做 alias，讓 class 改寫可以分批上。
- **絕對不要改** `--accent`、`--density`、`--font-sans`、`--font-mono` 的名字——`app.jsx:5-12` `applyTweaks()` 用 `setProperty` 寫到這些名稱，一改就靜默失效。
- **階段順序要反過來**：先做 JSX 精簡（縮小要重刻的表面），再換 token，再改 class rule，最後做 PDF 指定版面的 JSX 重排（login 卡片、inline trace、citation drawer）。

---

## 要修改的檔案

### 核心樣式 + 版面表面
- **`d:\ANILA\ANILA_UI\anila-ui\src\styles.css`** — token 區塊重寫（`:root` + 新增 `[data-theme="dark"]`）、各 feature domain 的 class rule 重寫、body gradient token 化、scrollbar token 化、宣告 `color-scheme`。
- **`d:\ANILA\ANILA_UI\anila-ui\src\app.jsx`** — 拆掉機密 sticky 橫幅 wrapper、PAGE header；把機密狀態縮成 top bar 裡 API key pill 旁邊的小 pill；**不動** `applyTweaks()`、`_aUS`/`_aUE` hook alias、`window.ANILA_TWEAKS`、`/*EDITMODE-BEGIN*/` 標記（host harness 用字串比對去改這些，改到就壞）。
- **`d:\ANILA\ANILA_UI\anila-ui\src\chat.jsx`** — 拆掉 empty state 裡的 WAVE 卡片，換成目標截圖的 4 張 suggestion card；拆掉 assistant 訊息上方堆疊的 TRACE/SOURCES/CONFIDENCE 摘要盒，改成 PDF page 3 的 inline collapsible 路由追蹤條（header line：`⇄ routing trace · N steps · completed`，展開顯示 `router.classify / retriever.search / llm.generate / postprocess.cite`）。
- **`d:\ANILA\ANILA_UI\anila-ui\src\trust.jsx`** — 右側 citations drawer 對齊 PDF page 4（前 3 筆來源卡片含 % 相關度 bar、metadata `§ section · updated YYYY-MM-DD`、snippet 預覽；訊息內 `[1][2][3]` 上標可點，開 drawer 並定位到該筆）。
- **`d:\ANILA\ANILA_UI\anila-ui\src\login.jsx`** — 對齊 PDF page 2 版面：左側歡迎卡（`歡迎使用 ANILA` 標題 + 說明 + 2×2 狀態卡），右側登入表單卡（帳號 / 密碼 / CSP API Key + 「登入 ANILA」按鈕 + `local · ldap · sso` 切換）。
  - **狀態卡調整（內網本地部署、無配額）**：保留 `5 可用 agents`、`24.3K 今日 tokens`、`142ms 平均 latency`；第 4 張把 PDF 原本的 `∞ 剩餘配額` 換成 **`5/5 healthy` agent 健康狀態**（或其他本地部署有意義的指標，例如 `runtime · online`、`uptime 99.9%`），跟內網情境對齊。
- **`d:\ANILA\ANILA_UI\anila-ui\src\components.jsx`** — 輕觸：確認 `Button` 的 `onMouseEnter/onMouseLeave` 還是引用換完後存在的 token（目前引用 `--bg-subtle`，換完後會存在）。共用的新 class 原語（`.mono-label`、`.ui-chip`）若有也放這。

### 次要表面（只換皮、不改版面）
- **`src\multiagent.jsx`** — compare 對照視圖，只換 token
- **`src\collab.jsx`** — share dialog + handoff，只換 token
- **`src\tweaks.jsx`** — settings drawer，只換 token

### 只讀參考（不改）
- `d:\ANILA\ANILA_UI\ANILA_templete\anila-ui\index.html` — token 權威來源
- `d:\ANILA\ANILA_UI\ANILA_templete\anila-ui\src\*.jsx` — inline style 寫法參考
- `d:\ANILA\ANILA_UI\scraps\feature-showcase-page{1-4}.png` — 視覺驗收目標

### 絕對不要動
- `src/runtime/`（auth、api、sse SDK）— 純邏輯，Phase 0 會先 grep 有沒有引用 class 字串
- `src/data.jsx`（fixtures、`generateFakeResponse`、`chunkText`）— 純資料
- `src/icons.jsx`（用 `currentColor`，自動跟主題）
- `src/main.jsx`、`vite.config.js`、`vitest.setup.js`、`package.json`、`Dockerfile`、`docker/`、`dist/`、`node_modules/`

---

## 階段順序（每個階段可獨立驗證）

### Phase 0 — 只讀稽核（不動程式碼）
- Grep `src/runtime/` 有沒有 CSS class 名字串（例如 `message-card`、`citation-card`）。如果 SSE renderer 或 auth 程式碼引用了我要改的 class，先在 Phase 3 之前 flag。
- Grep 所有 `.jsx` 裡的 `var(--` 列出 JSX 真正吃的 CSS 變數，建立改名對照表。確認沒有程式碼讀 `--ink` / `--line` / `--surface` / `--bg-soft`；有的話加 alias。
- 確認 `tweaks.jsx` 只寫 `--accent`、`--density`、`--font-sans`、`--font-mono` 這四個。

### Phase 1 — JSX 精簡（先拆掉 PDF 沒有的家具）
- `app.jsx`：拆掉 `PAGE 03 · RUNTIME` header；拆掉 sticky 機密橫幅 wrapper；在 top bar API key chip 旁塞一個精簡版機密 pill。
- `chat.jsx`：刪 empty state 的 WAVE 卡片條；刪 assistant 訊息上方堆疊的 TRACE/SOURCES/CONFIDENCE 摘要盒；為 inline collapsible trace 條先搭骨架（真正樣式在 Phase 3 做）。
- 驗證：app 還能啟動、還能串流、citation `[n]` 上標還會 render（drawer 還沒接沒關係）。

### Phase 2 — Token 重寫
- `styles.css` `:root` — 換成 templete oklch 調色盤；採用 `--fg` / `--border` / `--bg-elev` 等命名；加舊名 alias `--ink: var(--fg)`、`--line: var(--border)`、`--surface: var(--bg-elev)`、`--bg-soft: var(--bg-subtle)`。
- `styles.css` 新增 `[data-theme="dark"]` 區塊，對應 templete 的 oklch 深色覆寫。
- `styles.css` token 化 body gradient（把 `linear-gradient(180deg, #f7f4ee, #f1ece4)` 換成平底 `var(--bg)` 或 token 化漸層）；token 化 scrollbar（用 `var(--border-strong)` 取代 hex 字面值）；加 `color-scheme: light dark`（透過 `[data-theme]` 切換）。
- 驗證：淺色是近白、深色能切換（以前是 no-op）、tweaks 的 accent/density/fonts 還能即時反應。

### Phase 3 — Class rule 改寫（按 feature domain 分批，每批自成一單元）
1. Sidebar + top bar（`.sidebar-*`、`.runtime-shell`、`.icon-button`、`.api-key-chip`）
2. Chat empty state + message card + assistant trace 條（`.empty-state-*`、`.message-*`、`.assistant-*`、`.trace-*`）
3. Citations drawer + citation card + 相關度 bar（`.citations-drawer`、`.citation-*`、`.confidence-chip`）
4. Composer + 附件 + `@agent` 提及（`.composer-*`）
5. Compare 對照視圖（`.compare-*`）
6. Share dialog + handoff + workspace（`.share-dialog-*`、`.handoff-*`、`.workspace-*`）
7. Login（`.login-*`）— 只改幾何樣式，完整版面重排在 Phase 4
8. Modals + settings + chips + 雜項（`.modal-*`、`.settings-*`、`.follow-up-chip`、`.classified-banner` → 精簡 pill 變體、`.empty-state-*`）

### Phase 4 — PDF 指定的 JSX 重排
- `login.jsx`：左側歡迎卡（標題 + 說明 + 2×2 狀態卡，第 4 張用 agent 健康狀態取代 PDF 原本的配額卡）+ 右側登入表單卡 + 認證模式切換列。保留 auth flow、localStorage 復原、LDAP/SSO dispatch——只改 DOM 版面。
- `chat.jsx` + `trust.jsx`：接 inline collapsible 路由追蹤；接可點的 `[n]` citation 上標開 citations drawer 並定位到該筆。

### Phase 5 — 清理
- Phase 3 全部結束後、確認沒人再用舊名後，移除舊 token alias（`--ink`、`--line`、`--surface`、`--bg-soft`）。
- 刪掉 Phase 1 拔掉家具後孤立的 CSS class（`.wave-card`、`.page-header`、舊 `.classified-banner`）。
- 跑下面完整驗證流程。

---

## 要保留、不能動的邏輯/工具

- `src/runtime/auth.jsx` — AuthProvider、JWT/localStorage 復原、LDAP/SSO dispatch。Phase 4 的 login 表單必須呼叫現有的 `signIn()` / auth context。
- `src/runtime/sse.js`、`src/runtime/api.js` — 串流 + HTTP client。Chat 訊息流要繼續呼叫這些。
- `src/data.jsx` — `AGENTS`、`INITIAL_CONVERSATIONS`、`FOLDERS`、`generateFakeResponse(text, agentId)`、`chunkText(body)`。Phase 1 的 suggestion card 可以從 `INITIAL_CONVERSATIONS` 拉，或在 `data.jsx` 加一個小 `SUGGESTION_PROMPTS` 常數。
- `src/icons.jsx` — `Icon`、`AnilaGlyph`、`IconSend`、`IconPlus`、`IconAttach`、`IconAt` 等。全用 `currentColor`，原樣使用。
- `src/app.jsx` `applyTweaks(t)`（5–12 行）— CSS 變數 runtime 寫入邏輯。**不要動**，這是 runtime 客製化的 public API。
- `src/app.jsx` `streamResponseInto(...)`、`sendMessage(...)`、`enterCompare(...)`、compare mode state — 保留 wiring。
- `window.ANILA_TWEAKS` 和 `/*EDITMODE-BEGIN*/ ... /*EDITMODE-END*/` 標記（index.html 裡）— host harness 用字串比對編輯，不要改名或搬位置。
- `_aUS` / `_aUE` / `_aUR` / `_aUC` hook alias（app.jsx）— 專案慣例（templete 時代跨 `<script>` 載入避免 React hook 名稱碰撞），Vite port 雖然不需要，但保留慣例不刪。

---

## 風險與對策

| 風險 | 對策 |
|------|------|
| Token 改名把 `components.jsx` 的 hover/leave inline handler（`e.currentTarget.style.background = "var(--bg-subtle)"`）弄壞 | 直接沿用 templete token 名（`--bg-subtle` 等）；Phase 0 grep 驗證 |
| `applyTweaks` 寫 `--accent` / `--density` / `--font-sans` / `--font-mono`，改名會讓 tweaks 面板靜默失效 | 這四個名字絕對不動，視為 public API |
| `runtime/` SDK 如果引用 class 名 → SDK 掛 | Phase 0 grep；有的話擴充改名對照或保留原 class 名 |
| 深色模式目前是 no-op，oklch 深色區塊是新程式碼不是移植，第一次上一定會有 bug（對比度、表單控制、scrollbar） | Phase 2 驗收時操作深色切換；加 `color-scheme` 讓原生控制跟著走 |
| Body gradient（`styles.css:39-41`）寫死，深色模式會漏米色 | Phase 2 token 化成 `var(--bg)` 平底或 token 化漸層 |
| Scrollbar thumb 寫死 `rgba(20,24,29,0.18)`，深色不對 | Phase 2 token 化成 `var(--border-strong)` |
| `window.ANILA_TWEAKS` / EDITMODE 標記在 JSX 編輯時意外被搬走 → host harness 改寫流程斷 | 這些 block 一個字不改、物件字面值保持原樣 |
| `_aUS`/`_aUE` alias 看起來像 dead code，被重構掃掉 → 全檔都壞 | 保留慣例 |
| 字型換（IBM Plex → Inter / JetBrains Mono）造成中文字形 regression | 兩個 stack 都把 `Noto Sans TC` 放第一位；`index.html` 的 Google Fonts link 要包含三個 family |
| `dist/` 上版沒重 build → CSS 改不會生效 | 確認 deploy 流程會跑 `npm run build`；不要手改 `dist/` |
| component 的 inline style（`style={{ ...base, ...variants }}`）specificity 會贏過 `.ui-button` class rule | Class rule 只管 inline style 表達不出的 pseudo-state（`:hover`、`:focus-visible`、`:disabled`、`[aria-pressed="true"]`） |
| Login 狀態卡拿掉配額卡之後，要選一個內網情境有意義的替代（agent 健康 / uptime / runtime online）— 選錯會傳遞錯誤訊息 | 預設採 `5/5 healthy` agent 健康；可在 ExitPlan 階段調整指標 |

---

## 驗證

### 每個 phase 做完
- `npm run dev` — 啟動無錯、無 missing-module 警告
- 手動走一遍該 phase 影響的關鍵 flow

### 全流程（Phase 5 後）
1. **視覺驗收** — 跟 `scraps/feature-showcase-page{1-4}.png` 左右比對：
   - Page 1 — 品牌著陸 header（如果有單獨渲染；否則確認 top-bar brand glyph 吻合）
   - Page 2 — login shell 版面 + 狀態格子（**第 4 張用 agent 健康狀態，非配額**）+ 表單 + 模式切換
   - Page 3 — runtime shell：sidebar（logo、新對話、tabs、資料夾 chip、搜尋、最近對話含 tags、底部 user profile）+ 主區（top bar 有 API key pill + 機密 pill + compare + settings + 主題切換；chat card 有 inline 路由追蹤條和 inline `[1][2][3]` 引用）+ composer（target 選擇 + textarea + 附件 + @提及 + 送出）
   - Page 4 — 點 `[n]` 開 citations drawer，顯示前 3 筆來源、% 相關度 bar、`§` section 標籤、updated 日期、snippet 預覽

2. **Tweaks 面板操作**（最容易靜默壞的表面）：切深色、改 accent 顏色、改密度、改 trace style（`collapsible` vs `inline`）、改字型 family。每一個都要視覺上即時反應且無 console error。

3. **Auth round-trip**：login → sidebar 帶使用者 → logout → 回 login → 再登入會復原。確認 `applyTweaks` + `AuthProvider` + localStorage 都沒壞。

4. **Chat 黃金路徑**：empty state → 點 suggestion card → 串流出現 → 路由追蹤填入 → 點 citation `[n]` 開 drawer → 切機密 → compare mode split view → share dialog → handoff flow。

5. **`npm test`** — vitest 全過（`runtime/components.test.jsx`、`runtime/sse.test.js`）。這些不測 CSS 應該不會受影響，但要確認 Phase 1/4 沒造成 JSX 邏輯 regression。

6. **`npm run build`** — production build 成功、無 dead-import 後遺症、bundle size 沒爆炸。

7. **Spot-check `components.jsx` hover/leave**：hover default Button、primary Button、subtle Button，淺色/深色模式下背景 transition 都對。

---

## 本次不做（non-goals）

- 不把 Vite app 改成 templete 那種 100% inline-style，class 架構保留。
- 不改 `runtime/` SDK、資料 fixtures、auth flow。
- 不動 backend（`myCSPPlatform/`）、docker、deploy config。
- 不加現有 app 之外的新路由或新功能。
- 不在現有 vitest 之外新增測試。
- **不接後端的 conversation / attachment / handoff / public-share API 到 UI**（這些 server 端上個 session 已完成，但前端串接是另一坨工作）。
- **不加入任何配額/計費 UI**——本專案為企業內網本地部署，沒有配額概念。
