# ANILA Roadmap

> **唯一前瞻路線圖。** 已完成見 [`CHANGELOG.md`](../CHANGELOG.md)；引擎內部見 [`../anila-core/ROADMAP.md`](../anila-core/ROADMAP.md)。
> 更新 2026-06-30。**本檔已從「功能菜單」收斂成「一條有順序的 ANILA v1 主幹 + 凍結清單」**（2026-06-30 與 user 對齊產品定位後重排）。

---

## 0. 產品定位（北極星）

ANILA 是**雙產品組合**（如 Google 的 Gemini 與 NotebookLM：共用模型底座、兩個不同產品）：

| 產品 | 類比 | 定位 | 狀態 |
|---|---|---|---|
| **ANILA**（ANILA UI） | **Gemini** | 通用 agent 助理：LLM-as-Router + 註冊 agent，統一在 ANILA UI 使用（OpenWebUI 式）。**agent 是工具、ANILA 本體是平台** | **主核心 — 第一個 done** |
| **ANILALM** | **NotebookLM** | 以來源為本的文件研究：KB + Studio 產出 | **第二核心 — ANILA v1 done 後啟動** |
| 共享底層 | — | CSP / anila-core / ingestion / 模型 = **內部水電**，收斂到「兩產品需要的程度」 | 不鍍金成對外 SDK |

**鐵律**：現在**只追 ANILA v1**。其餘一律凍結（見 §3）。

---

## 1. 🎯 ANILA v1 — 「Done」的定義

> 一個開發者 clone 模板建好 agent → 註冊、admin 核准 → 它出現在 ANILA UI → user 在 ANILA UI 問問題（**可靠 Router 自動路由，也可自己選 agent**，沿用現行雙模式）→ Router 直答或派給對的 agent → agent 對綁定 collection 做 RAG → **有來源、可信的答案串流回 ANILA UI**（看得到引用 + 跑了哪個 agent/工具）→ 且在真實內網模型（.12 / gpt-oss）上穩定、資安 gate 關好。

驗收清單（= 做完）：
- [ ] **開發者 on-ramp 通**：clone → `register`（不再 422）→ approve → **出現在 ANILA UI 可用 agent 清單**
- [ ] **Router 路由可靠**：直答 + dispatch 都行；**無 MaxTurns 死路**（強制收尾）；abort + retry；multi-hop **帶 context**
- [ ] **ANILA UI 信任可見**：串流 + **可點引用** + 來源面板 + **跑了哪個 agent/工具**（ToolWidget）+ **真 confidence**
- [ ] **預設 grounded**：答案帶來源（agent RAG）+ 關聯來源
- [ ] **usage 全歸戶**（補非串流 dispatch 洞）
- [ ] **資安 gate 關好**（R-SEC-1~5）
- [ ] **真模型端到端 smoke 綠**

> 這條主幹**幾乎全是「接線 + 可靠性 + 資安」，不是新功能**——東西大多建好了。這就是收斂。

---

## 2. 🔫 ANILA v1 — 開槍順序（按驗收項垂直切，**不按 repo**）

依賴堆疊（實測呼叫圖）：**ANILA UI ──/router SSE──▶ anila-core(Router) ──CSP_BASE_URL──▶ CSP（LLM/dispatch/search）**。
CSP 是地基（大家都打它）；**anila-core 與 ANILA UI 是同一條 SSE 契約的兩端**，要成對做。

### Stage 1 — CSP 地基補洞（資料面先可信；多半已建好）
- **R-SEC-1** `.env.bak.*` 進 `.gitignore`（PUBLIC repo 祕密外洩，最便宜先做）
- **R-SEC-2** 分類 latch IDOR（共用碼，**main 起 → cherry-pick 全分支**）
- **R-SEC-3** `CARD_DEV_SKIP_NONCE_BINDING` prod fail-fast guard
- **R-SEC-4** `deploy-prod.sh` preflight 補內網必填 env + `docker compose config`
- **R-SEC-5** `.12` gateway smoke 納 deploy gate（DNS/extra_hosts + TLS verify + /v1/models）
- 補**非串流 dispatch usage 記帳**（usage 歸戶洞）
- CSP search **server 端備好 `expand_relations`**（R-WIRE-1 後端側）
- registry / approve / **`/v1/agents` list** 打磨；**修 `register` CLI 422**（送 `base_model_id`）

### Stage 2 — 凍結 SSE 契約（Router ⟷ UI 的接縫）
- 定義並凍結 `/router/v1/chat/completions` 事件 shapes：`answer delta` / `tool_call_started·finished` / `citation{doc_id,chunk}` / `confidence` / `typed-terminal`(completed｜max_turns｜aborted｜budget｜length｜error) / `usage`。**兩邊照這份契約 lockstep。**

### Stage 3 — anila-core Router 可靠性（產生 UI 要渲染的事件）
- **max-turns 強制收尾**（tool_choice='none' 要模型收尾，別空答）
- **honor abort**（`abort_signal` + FastAPI `is_disconnected`）
- **`_api_call` bounded retry/backoff** + 分離 connect/read/total timeout
- **typed-terminal** 進 SSE；確保上面那些事件**真的被 emit**
- **multi-hop dispatch 帶 context**（`context_messages`/`handoff_meta`）
- ↳ 細節見 `anila-core/ROADMAP.md` Phase 1/3（這幾項現在做；其餘引擎工作凍結）

### Stage 4 — Router emit ⟷ ANILA UI bind（垂直信任切片，一個功能一刀）
- **on-ramp**：register → 出現在 ANILA UI 可用清單
- **agent picker（雙模式）**：沿用現行——可**靠 Router 自動路由**、也可**手動選 agent**；`/v1/agents` 餵清單、打磨切換 UX
- **R-WIRE-2** ToolWidget 接 SSE（Router 已 emit，`app.jsx` 綁上）
- **R-WIRE-3** confidence 真算（檢索分數）+ UI 渲染（`ConfidenceChip` 目前收 None）
- **R-WIRE-4** parent_content「展開段落」+ **可點引用** + 來源面板
- **R-WIRE-1**（client）：答案顯示「相關來源」

### Stage 5 — 真模型端到端 smoke（= done）
- 全鏈在 `.12` / gpt-oss 跑通：register→approve→ANILA UI 問→Router 直答/派送→grounded 串流答（引用+ToolWidget+confidence）+ usage 歸戶 + SSE 逐塊。

### ✅ 已定：agent 選擇 = 兩者都要（沿用現行）
- ANILA UI 保留**雙模式**：可**靠 Router 自動路由**，也可 **user 自己選 agent**（OpenWebUI 式 picker）。
- 影響：Stage 1 的 `/v1/agents` list 要餵 picker（可用 agent 清單）；Stage 4 加「agent picker UX 打磨」。
- 註：顯示「Router **為何**選此 agent」的 dispatch 決策 ribbon 屬 v2 polish（§3c）；**手動選 agent 本身已在 v1**。

---

## 3. 🧊 凍結（ANILA v1 之外，**現在不碰**）

### 3a. 第二核心 ANILALM（ANILA v1 done **之後**啟動，非刪除）
來源檢視器（pdf.js）、一鍵導讀、學習指南/FAQ、文件並排比較、跨 collection 提問、自動詞彙表、Notes 面板、三欄工作區、Studio 即時預覽、入庫狀態板、**per-collection 關聯地圖（R-WIRE-8）**、**GraphRAG**（檢索層深化，multi-hop+community）、**知識圖探索器**（admin/治理）、協作/KM（projects / `collection_access_grants` / team-memory / 標註餵 RAG / saved-search 站內通知）、Studio 產出（決策備忘錄 / 比較矩陣 / citation-linked / artifact store+版本化 / 流程圖）。

### 3b. 引擎/平台 sprawl（內部水電，別鍍金）
- **anila-core 對外 SDK 化**：公開 API 凍結 / packaging / god-module **完整**解構 → 凍（單位是 clone `anila-agent` 模板、非直接用 anila-core；它是 Router 內部引擎）。**只做 §2 Stage 3 要的可靠性那幾項。**
- **重型 orchestration**：coordinator / 複雜 handoff / **deep-research-as-chat** / MCP → 凍（ANILA v1 只要簡單 dispatch）
- **統一 runtime workspace** → **收回**（與雙產品策略相反）

### 3c. ANILA v2 / 之後（仍是 ANILA、但非 v1 阻擋）
composer faceted 過濾、query 精修 chips、regenerate-and-compare、對話整理（資料夾/釘選/封存）、dispatch 決策 ribbon、對話一鍵變 Studio、排程 agent、核可式 agent run、agent 範本清單 + 從 collection 建 agent 精靈、grounded answer mode / 檢索診斷。

---

## 4. 🌏 跨產品策略（Later — 兩產品都受益、但**不阻擋** ANILA v1）

1. **一般文件 ingestion / parsing 品質**（table/figure-aware、OCR air-gap、per-type 路由）— RAG 真天花板；**對 ANILA v1 答案品質也有幫助，視情況提前**。
2. **中英跨語檢索**（query 翻譯 / 縮寫·英文詞 aliasing，全 on-prem）。
3. **離線/降級模式 UX**（模型掉 → 快取檢索/排隊）— 唯一純 on-prem 需求，**對 ANILA v1 韌性有益**。
4. 文件生命週期/留存；品質擁有權下放（collection owner golden-question）。
5. `anila-tokens` 三前端共用 design-token（含 a11y/高對比）。
6. agent fleet 治理/可觀測（**註**：規則目前全 agent-local、平台無中央政策控制面，自主 vs 治理取捨）；Phase 7 productionization；cross-branch parity CI；版本化發布物 + air-gap release SOP。

---

## 5. 分支同步原則（採用 Codex card review）
- `main` 為 SSOT，但 **`prod-intranet-card` 的 card/SSO 熱區手動檔案級 port**，不可被 `main` 整檔覆蓋：`auth.py`、`users.py`、`auth_providers.py`、`models/user.py`、`card_auth*.py`、`external_auth_service.py`、`schemas/card.py`、`LoginView.vue`、nginx card/Host allowlist。
- port 前看 `git diff origin/main...prod-intranet-card` + `git cherry`。
- **例外**：`R-SEC-2`（分類 IDOR）是共用碼，從 `main` 起修並 cherry-pick 全分支。

---

## 6. 已整併/退役（本檔取代之）
- 已刪：`docs/planning/roadmap-2026h2-intranet.md`、`roadmap-2026h2-intranet-card.md`（內容入本檔）。
- 已歸檔 `docs/archive/*.SUPERSEDED.md`：`anila-agent-enhancement-roadmap`、`agenticrag-*`。
- 保留（非 roadmap 作業檔）：`docs/planning/sprint-7x-plan.md`、`branch-sync-backlog`（待刷新）。
- IC-7（codeserver/n8n/gitlab dev-tool ingress 策略 + runbook doc-drift）**暫緩**，待 user 拍板。

*依據：card 雙稽核 + 功能/UIUX 多 lens 發想 + anila-core/anila-agent 機制盤點 + 與 user 對齊雙產品定位（ANILA=主核心先 done）。前瞻 TODO；已完成見 `CHANGELOG.md`。*
