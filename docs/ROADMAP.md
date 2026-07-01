# ANILA Roadmap

> **唯一前瞻路線圖。** 已完成見 [`CHANGELOG.md`](../CHANGELOG.md)；引擎內部見 [`../anila-core/ROADMAP.md`](../anila-core/ROADMAP.md)。
> 更新 2026-07-01。**本檔已從「功能菜單」收斂成「一條有順序的 ANILA v1 主幹 + 凍結清單」**（2026-06-30 與 user 對齊產品定位後重排；2026-07-01 標註 Stage 1–4 進度）。
> ⚠ §0 產品定位另有一份 Codex 主導的「產品核心重新定位」設計在**獨立分支**進行中（proposal 見 `docs/product/anila-product-convergence-design.md`），可能修訂 §0（尤其「雙產品 vs 單一入口」）；在其定案前，本檔 §0 為當前 SSOT。

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

驗收清單（= 做完；**2026-07-01 進度標註**，各項細節見 §2 對應 Stage）：
- [ ] **開發者 on-ramp 通**：clone → `register`（✅ 不再 422）→ approve → **出現在 ANILA UI 可用 agent 清單**（UI picker ✅；剩驗 `/v1/agents` 餵清單）
- [ ] **Router 路由可靠**：直答 + dispatch 都行；**無 MaxTurns 死路**（✅ 強制收尾 done）；**abort**（⬜ 非 gated、下一刀候選）+ **retry**（⬜ 真需 Phase 2 重構）；multi-hop **帶 context**（⬜ Phase 4）
- [ ] **ANILA UI 信任可見**：串流 ✅ + 可點引用 ✅UI + 來源面板 ✅UI + 跑了哪個 agent（handoff ✅）+ 真 confidence（UI ✅、待生產端算）+ typed-terminal「為何停」（✅ Router+UI）→ **UI 全建好，缺口全在生產端 emit**（tool ToolWidget → v2、派送制，見 §3c）
- [ ] **預設 grounded**：答案帶來源（⬜ agent RAG 待填 `meta.citations`）+ 關聯來源（⬜ agent 端 `expand_relations` 待接）
- [ ] **usage 全歸戶**：✅ 非串流 dispatch 洞（P-3）；⬜ 串流 usage 真值
- [ ] **資安 gate 關好**（R-SEC-1~3 ✅ 已散全分支；R-SEC-4/5 → §4 deferred，非 v1 阻擋）
- [ ] **真模型端到端 smoke 綠**（⬜ Stage 5）

> 這條主幹**幾乎全是「接線 + 可靠性 + 資安」，不是新功能**——東西大多建好了。這就是收斂。**進度概覽**：Stage 1 ✅、Stage 2 ✅（契約凍結）、Stage 3 進行中（typed-terminal ✅ + max-turns ✅；abort/retry/multi-hop ⬜）、Stage 4 UI bind ✅（缺生產端 emit）、Stage 5 ⬜。

---

## 2. 🔫 ANILA v1 — 開槍順序（按驗收項垂直切，**不按 repo**）

依賴堆疊（實測呼叫圖）：**ANILA UI ──/router SSE──▶ anila-core(Router) ──CSP_BASE_URL──▶ CSP（LLM/dispatch/search）**。
CSP 是地基（大家都打它）；**anila-core 與 ANILA UI 是同一條 SSE 契約的兩端**，要成對做。

### Stage 1 — CSP 地基補洞（✅ **實質完成**，2026-06-30 → 已完成項見 CHANGELOG）
- ~~**R-SEC-1** `.env.bak*` 進 `.gitignore`~~ ✅ done
- ~~**R-SEC-2** 分類 latch IDOR（共用碼 main 起 → 全分支）~~ ✅ done（散全 7 分支）
- ~~**R-SEC-3** `CARD_DEV_SKIP_NONCE_BINDING` prod fail-fast guard~~ ✅ done（card）
- ~~**R-SEC-4** deploy-prod.sh preflight~~ → **§4 deploy-gate（非 card v1 主幹）**：`deploy-prod.sh` 是 public/military 用；card 兩支並存不刪（intranet-deploy 建置 + deploy-prod 維運）。
- ~~**R-SEC-5** `.12` gateway smoke 納 deploy gate~~ → **§4 deferred**：`.12` 已連通，此為韌性/回歸守衛，非 v1 阻擋。
- ~~補**非串流 dispatch usage 記帳**（P-3）~~ ✅ done（全分支）
- ~~CSP search server 端 `expand_relations`（R-WIRE-1 **後端**）~~ ✅ done（`fccf430`）。⚠ **agent 端消費未做**（`CspHttpRetriever` 不送/不讀 `related`）→ 併 **Stage 4「關聯來源」垂直切片**（對凍好的契約 + UI 一起做）。
- ~~**修 `register` CLI 422**（送 `base_model_id`）~~ ✅ done。**剩一項**：`/v1/agents` list 打磨（餵 UI agent picker）— 見 Stage 4 on-ramp（UI picker 已在，剩驗端點餵清單）。

### Stage 2 — 凍結 SSE 契約（Router ⟷ UI 的接縫）✅ **完成，契約已凍結**（2026-07-01 review 通過）
- 契約 [`docs/platform/router-sse-contract.md`](platform/router-sse-contract.md)：對碼驗證修正（passthrough 是活路徑、`anila.resumed` 有 emit `router_server.py:1187`）＋ typed-terminal **採方案 A**（獨立 `event: anila.terminal {reason,detail?}`，不碰 OpenAI `finish_reason`）。兩端 lockstep，改契約走契約文末流程。
- `anila.terminal` 的 emit（Router）+ onTerminal（UI）已於 **Stage 3 實作**（見下）；契約 shape 凍結不變。

### Stage 3 — anila-core Router 可靠性（產生 UI 要渲染的事件）
> **更正（2026-07-01 code-first 驗證）**：`anila-core/ROADMAP.md` 把 abort/max-turns/retry 都標「需 Phase 2 keystone」是**保守的**——實查 `query_engine.py` 後,**max-turns 補救 + honor abort 是 turn-loop 邏輯、不被 provider 重構 gated**;只有 **retry** 真的要 Phase 2（3 個 inline httpx `1232/1816/1955/2197` → 既有 `CSPPlatformProvider`，`next/L`）。
- ~~**typed-terminal** 進 SSE~~ ✅ **done**：`event: anila.terminal {reason,detail?}` Router 全出口 emit（`_with_terminal`，`completed`/dispatch `error`）+ ANILA UI `onTerminal`+badge。
- ~~**max-turns 強制收尾**~~ ✅ **done**（`60710cb`）：`query_engine.py` limit_check 撞上限時做 forced final turn（`_api_call(force_no_tools=True)`）→ 逼出真文字答案,終結「MaxTurns 死路」空答。TDD 綠、零 regression、**非 gated**。**剩 wiring**：engine `stop_reason='max_turns'` → Router `anila.terminal{reason:max_turns}` → UI badge。
- ⬜ **honor abort**（`query_engine.py:148` turn loop 加 abort 檢查 + FastAPI `is_disconnected`）— **非 gated**（loop 邏輯,已驗）,下一刀候選;完整取消 in-flight 呼叫則需 provider 可取消
- ⬜ **`_api_call` bounded retry/backoff** + 分離 connect/read/total timeout — **真需 Phase 2 provider 重構**
- ⬜ **multi-hop dispatch 帶 context**（`context_messages`/`handoff_meta`）— Phase 4，可並行
- ↳ 細節見 `anila-core/ROADMAP.md` Phase 2/3

### Stage 4 — Router/agent **emit** ⟷ ANILA UI bind（**UI bind ✅ 已完成，2026-07-01 實測**；缺的全在生產端 emit）
> **實測**：ANILA UI（`ANILA_UI/anila-ui`）是**接好線、在等資料的消費者**——`runtime/sse.js` 全解析、`app.jsx:applyMeta` 把 `meta.{citations,confidence,handoff_chain,usage,follow_ups,reasoning}` 全套用、`trust.jsx` 元件全建好（`CitationsDrawer`/inline `[N]`/`ConfidenceChip`/`FollowUps`/`AuditWatermark`/`HandoffTimeline`）。**UI 端幾乎無工可做；缺口全在「Router/agent 有沒有把資料放進 `anila.meta`」。** Stage 4 因此收斂成「生產端 emit」，UI 只剩 typed-terminal 一小塊 render。
- **on-ramp**：register → 出現在 ANILA UI 可用清單（UI ✅；驗 `/v1/agents` 餵清單）
- **agent picker（雙模式）**：UI ✅（picker 已在）；剩打磨切換 UX
- ~~**R-WIRE-2** ToolWidget 接 SSE（Router 已 emit）~~ → **descoped 至 v2**。Router **並未** emit tool_call（passthrough frozenset + `sse.js` callback 皆懸空無 producer）；ANILA 派送制，「跑了哪個 agent」已由 dispatch/handoff 呈現（見 §3c）。
- **R-WIRE-3 confidence**：**UI ✅**（`ConfidenceChip`+低信心追問已建、現全黑收 `None`）→ **缺生產端**：算 confidence（檢索分數→high/med/low）放進 `meta.confidence`
- **R-WIRE-4 引用/來源**：**UI ✅**（`CitationsDrawer`/inline `[N]` 已建並 wire）→ **缺生產端**：agent RAG 填 `meta.citations`（+parent_content 展開段落）
- **R-WIRE-1 關聯來源**：**UI ✅**（同一條來源面板）→ **缺**：agent 端 `CspHttpRetriever` 送 `expand_relations`、把 `related` 映成 citations
- **usage 浮水印**：**UI ✅**（`AuditWatermark` 顯示 trace/usage）→ **缺生產端**：串流 usage 真值（現歸零）
- ~~**typed-terminal**~~ ✅ **done**（Stage 3）：`sse.js` `onTerminal` + `chat.jsx`「為何停」badge（max_turns/error/aborted/budget）；Router 全出口 emit `anila.terminal`。**剩 wiring**：engine `stop_reason=max_turns`/abort → Router 對應 reason。

### Stage 5 — 真模型端到端 smoke（= done）
- 全鏈在 `.12` / gpt-oss 跑通：register→approve→ANILA UI 問→Router 直答/派送→grounded 串流答（引用+dispatch可見+confidence）+ usage 歸戶 + SSE 逐塊。

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

**Tool 能力（reserved／A-B 決策封存至 v2）**：ANILA v1 是**派送制**（Router 直答 or `DISPATCH:` 呼叫**一個** agent，文字標記 `_DISPATCH_RE` 非 function-calling），**無 tool_call**。未來若要 tool 能力，兩條路——**A**：tool 留在 agent 內，agent emit `anila.tool_call_*` → Router 轉發 → UI 顯示；**B**：Router 自己 function-calling（更貼 Gemini 式助理，但架構轉向較大）。**現在不選**——UI 端要渲染的 tool 事件形狀兩者相同，故 passthrough 管線（router `_AGENT_PASSTHROUGH_EVENTS` + `sse.js` `onToolCall*`）**保留並標 reserved**，v2 真做 tool 時再定 A/B。（同批 Sprint 13 scaffolded 但無 producer 的還有 spans/todos/interrupt 等，一併屬 v2。）

---

## 4. 🌏 跨產品策略（Later — 兩產品都受益、但**不阻擋** ANILA v1）

1. **一般文件 ingestion / parsing 品質**（table/figure-aware、OCR air-gap、per-type 路由）— RAG 真天花板；**對 ANILA v1 答案品質也有幫助，視情況提前**。
2. **中英跨語檢索**（query 翻譯 / 縮寫·英文詞 aliasing，全 on-prem）。
3. **離線/降級模式 UX**（模型掉 → 快取檢索/排隊）— 唯一純 on-prem 需求，**對 ANILA v1 韌性有益**。
4. 文件生命週期/留存；品質擁有權下放（collection owner golden-question）。
5. `anila-tokens` 三前端共用 design-token（含 a11y/高對比）。
6. agent fleet 治理/可觀測（**註**：規則目前全 agent-local、平台無中央政策控制面，自主 vs 治理取捨）；Phase 7 productionization；cross-branch parity CI；版本化發布物 + air-gap release SOP。
7. **`deploy-prod.sh` 自動生成祕密（public/military）**：缺就 `openssl rand` 生成 + persist `.env`（冪等），比照 `intranet-deploy.sh`；**card 不需要**（intranet-deploy 已生成）。共用碼、**main 起**。
8. **R-SEC-5 `.12` gateway smoke deploy-gate**（deferred；韌性/回歸守衛）：部署前對 `.12` 三道檢查——DNS/extra_hosts 解析（FQDN 非 IP）+ CSPKI TLS verify 到自簽 root + 帶 `MODEL_GATEWAY_API_KEY` 打 `/v1/models`——做成會擋部署的 preflight，接住「重跑 `intranet-deploy.sh` 重設 `ANILA_MODEL_CA_FILE`/旗標 → `.12` 信任靜默破掉」這類回歸。`deploy-prod.sh` 已有 `ANILA_REMOTE_MODELS=1` 探測可收斂。**`.12` 現已連通、無功能缺口**，待 gateway API key 正式簽發後一併補並在 `.12` 環境真驗三道綠。

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

*依據：card 雙稽核 + 功能/UIUX 多 lens 發想 + anila-core/anila-agent 機制盤點 + 與 user 對齊雙產品定位（ANILA=主核心先 done）。2026-07-01：對碼盤點各 Stage 實況（Stage 1 ✅ / 2 ✅ 契約凍結 / 3 typed-terminal+max-turns ✅ / 4 UI bind ✅）+ code-first 更正可靠性 gating。前瞻 TODO；已完成見 `CHANGELOG.md`。*
