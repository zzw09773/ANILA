# 移交文件 — Stage 2/3 typed-terminal + max-turns（2026-07-01）

> 交接對象：user / Codex / 未來 session。這 session 很長,做的東西都在**一條分支**上、**未推未併**。讀這份就能接手。
> 權威路線圖：[`docs/ROADMAP.md`](../ROADMAP.md)（本 session 已通盤更新到準）。SSE 契約：[`docs/platform/router-sse-contract.md`](../platform/router-sse-contract.md)（FROZEN）。

---

## 0. TL;DR

- 分支 **`feat/stage3-typed-terminal`**（off `main@a03eec3`,**8 commits,12 檔,+438/−30,未推未併**）。
- 完成：**Stage 2 SSE 契約凍結** + **Stage 3 的 typed-terminal（端到端）** + **max-turns 補救（兩個 runtime）** + **roadmap 通盤收斂** + **對抗式 review 的 3 個 HIGH correctness 修正**。全程 TDD、零 regression。
- 未完成（Stage 3/4 剩餘）：honor abort、retry（gated）、multi-hop context、Stage 4 生產端 emit（confidence/citations/related）。**都是實質多組件工程,見 §5。**

---

## 1. 分支 / Git 狀態（先讀）

```
feat/stage3-typed-terminal   (8 commits ahead of main, 未推 origin)
  23fbdd7 fix(stage3): review fixes — errors 不再誤標 completed, resume 補 terminal
  8e26731 feat(stage3): max-turns 補救 SDK path (anila-agent serving)
  162c168 docs: complete ROADMAP
  fce1ae8 docs(stage3): max-turns done; 更正 gating
  60710cb feat(stage3): max-turns 補救 — forced final answer (query_engine)
  5b6bcc4 docs(stage3): typed-terminal done; mark gating
  903e311 feat(stage3): typed-terminal consumer (ANILA UI) + error reason
  f8224a3 feat(stage3): emit anila.terminal — Router producer
```

- **共用碼**：`router_server.py` / `query_engine.py` / ANILA UI / `service_wrapper.py` 都是共用碼。照分支模型（`AGENTS.md §3` / `docs/ROADMAP.md §5`）**從 `main` 起 → cherry-pick 全分支**。目前全在 feature 分支,**併回 main + 扇出**是待辦（user 決定何時）。
- ⚠ **工作樹有 5 個未提交 `ANILALM/*.tsx`（Codex 的產品收斂重新命名,ANILA LM→我的知識庫 / 文件→檔案）**。**不是本 session 的、別 commit 進這條分支**;Codex 本該在別分支做。交接時請 user 把它們移到 Codex 的分支或丟棄。
- 接手指令：`git switch feat/stage3-typed-terminal`（工作樹乾淨前先處理上面那 5 個檔）。

---

## 2. 這 session 交付了什麼

### Stage 2 — SSE 契約凍結 ✅
- `docs/platform/router-sse-contract.md`（FROZEN）：對碼盤點 Router 實際 emit 的每個事件,釘死 shape。修正過草稿的錯（passthrough 是活路徑、`anila.resumed` 有 producer）。
- **typed-terminal 採方案 A**：獨立 `event: anila.terminal {reason, detail?}`（不污染 OpenAI `finish_reason`）。reason ∈ {completed｜max_turns｜aborted｜budget｜length｜error}。

### Stage 3 — typed-terminal（端到端）✅
- **Router producer**：`_make_terminal` + `_with_terminal(gen)` wrapper,套在**全 5 個 StreamingResponse 出口**。預設 `completed`;掃到非-registry 的 error trace → `error`;generator 可 pre-emit 覆寫（max_turns 等）。`[DONE]` 用**寬鬆比對**（resume 的 `aiter_lines` 會剝行尾）。
- **ANILA UI consumer**：`sse.js` `onTerminal` → `app.jsx` 存 `msg.terminal` → `chat.jsx` render「為何停」badge（completed/length 靜默）。
- 檔：`anila-core/.../router_server.py`、`ANILA_UI/anila-ui/src/{runtime/sse.js,app.jsx,chat.jsx}`。

### Stage 3 — max-turns 補救（**兩個 runtime**）✅
> **關鍵架構**：有**兩套 runtime**——① anila-core `QueryEngine`（`api/server.py` 用）② openai-agents **SDK**（實際 anila-agent 模板 `service_wrapper.py` 用）。max-turns 要**各補一次**。
- **QueryEngine**（`query_engine.py`）：limit_check 撞上限時做 forced final turn `_api_call(force_no_tools=True)`（tools=[] 逼文字答案）→ 終結「MaxTurns 死路」空答。
- **SDK path**（`service_wrapper.py`）：catch `MaxTurnsExceeded` → forced tools-off 答案（`run_once(agent.clone(tools=[]), max_turns=1)`）+ emit `anila.terminal{max_turns}`（Router passthrough 轉發 → wrapper dedup → UI badge）。

### Roadmap 通盤收斂 ✅
- `docs/ROADMAP.md`：§1 驗收清單對齊各 Stage 進度、修 Stage3↔Stage4 矛盾、更正可靠性 gating、標註 §0 有 Codex 平行重設計。

### 對抗式 review 修正 ✅（見 §4）

---

## 3. 目前 v1 Stage 狀態（權威見 ROADMAP §2）

| Stage | 狀態 |
|---|---|
| 1 CSP 地基 | ✅ 完成（散全 7 分支） |
| 2 凍結 SSE 契約 | ✅ 完成（frozen doc） |
| 3 Router 可靠性 | 🟡 **typed-terminal ✅ + max-turns ✅**;⬜ abort（非 gated）、retry（真需 Phase 2 重構）、multi-hop（Phase 4） |
| 4 UI bind | ✅ UI 全建好（消費端就緒）；⬜ **缺生產端 emit**：confidence/citations/related（agent 端 RAG provenance）、串流真 usage |
| 5 真模型 smoke | ⬜（需 live `.12`） |

---

## 4. 對抗式 multi-agent review（本 session 尾端跑）

21 agents、對抗式驗證,15 CONFIRMED（7 HIGH / 8 MEDIUM）+ 7 low/nit,1 假警報被反駁。

**已修（HIGH correctness,commit `23fbdd7`）**：
1. **errors 誤標 `completed`** → wrapper 掃 error trace 標 `error`（排除非致命 registry 警告）；SDK path generic error 也 emit `error` terminal。**兩 runtime**。
2. **resume 收不到 terminal**（`aiter_lines` 剝行尾）→ 寬鬆 `[DONE]` 比對。
3. 附帶修好一個**遮蓋 error 的既有測試**（沒 mock `_stream_llm_sse`）。
4. 補測試：force_no_tools 真的關 tools。

**未修（追蹤清單,非 correctness）**：
- HIGH #6：dispatch error 整合測試（機制已由 wrapper unit test 覆蓋,整合錦上添花）。
- HIGH #7：`chat.jsx` badge render 測試（要架 `MessageBubble` render harness;badge 邏輯簡單低風險）。
- MEDIUM（wiring 完整性）：① anila-core runtime `server.py` 的 max_turns/budget `stop_reason` 還沒 emit terminal（只有 SDK path 有）② agent **非串流 / multi-turn dispatch** 路徑沒 max-turns terminal ③ `aborted`/`budget`/`length` reason 未實作 ④ dispatch 串流 terminal 後還有 recomposed 內容的順序 nit。
- 完整報告：workflow run `wf_641ff4dd-eb3` 的 output（transcript dir 在 `.claude/.../subagents/workflows/`）。

---

## 5. 下一步（接手者，依優先）

1. **併回 main + 扇出**（共用碼,user 決定何時 push）。先處理工作樹的 Codex ANILALM 檔。
2. **honor abort**（非 gated,roadmap 標的下一刀）：`AgentContext.abort_signal`/`is_aborted()` 已存在但 `query_engine.py:148` turn loop 不檢查;Router 串流加 `request.is_disconnected()`。**注意雙 runtime + 完整取消 in-flight 需 provider 可取消（那部分 gated）**。
3. **Stage 4 生產端 emit**（點亮已建好的 UI）：confidence（檢索分數→level）、citations（agent RAG 填 `meta.citations`）、related（`CspHttpRetriever` 送 `expand_relations`）。**SDK-based agent serving 目前根本不 emit `anila.meta`**——要讓它捕捉 RAG 工具結果 → 組 meta → emit。這是一整個 RAG-provenance 功能。
4. **retry/backoff**（真 gated）：Phase 2 keystone 重構——`router_server.py` 4 個 inline httpx（`1232/1816/1955/2197`）→ 既有 `CSPPlatformProvider`;先寫 characterization tests。見 `anila-core/ROADMAP.md` Phase 2/3。
5. review 追蹤清單（§4 未修項）。

---

## 6. 怎麼跑測試（有坑,務必照做）

- **anila-core**：沒自己 venv,用 backend venv + PYTHONPATH,**且從 `anila-core/` 目錄跑**（否則 repo 根 `.env` 被 pydantic-settings 讀進去 → Settings `extra=forbid` 全炸）：
  ```
  cd anila-core && PYTHONPATH=src ../myCSPPlatform/backend/.venv/bin/python -m pytest tests/ -q
  ```
- **anila-agent**：**有自己的 venv**（`agents` SDK 在那）：`cd anila-agent && .venv/bin/python -m pytest tests/ -q`
- **ANILA UI**：`cd ANILA_UI/anila-ui && npx vitest run`（+ `npm run build` 驗前端,別只 vitest）。
- **已知 pre-existing 失敗**（非本 session,別當 regression）：anila-core 6 個——`test_g3_*`×2（既有 SQL 架構違規 + grep 找不到 AgenticRAG）、`test_router_runtime_contract`×2（fake 過時缺 `forwarded_headers`）、`test_dispatch_pins_owning_agent`（順序 flaky,單獨跑 pass）、`test_chunking_plugins` 1 個。用 worktree 對 base 核對過。

---

## 7. 踩過的坑 / 教訓（省後人時間）

- **雙 runtime**：QueryEngine vs openai-agents SDK。可靠性/emit 的活常要做兩次。
- **文件落後 code ~10 sprint**：以 code 為基線盤點,別照 roadmap 舊字面（本 session 靠這翻案了「R-WIRE-1 後端已完成」「ANILA UI 已建好」「abort/max-turns 非 gated」）。
- **git 髒樹三連**：① subagent 與主線**共用工作樹**,跑 `git checkout` 會切走你的分支 ② `git stash` 復活舊孤兒 ③ resume/壓縮後工作樹可能把「已 commit 工作」整包反轉。**鐵則：每次 session 開始/resume 先 `git status`,見 M 檔先 `git diff` 判斷是不是已 commit 工作的反轉。**
- **ANILA 是派送制、非工具制**：Router 直答 or `DISPATCH:` 呼叫一個 agent（文字標記 `_DISPATCH_RE`,非 function-calling）。`tool_call`/spans/todos/interrupt 是**保留的 latent 事件**（有 passthrough 管線、無 producer）。見 ROADMAP §3c。
- 秘密零外洩（PUBLIC repo）;繁中台灣用語。
