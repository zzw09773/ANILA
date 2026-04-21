# AgenticRAG 樣板 — 規劃說明

> **Source of truth 是平台根的 [`../anila_plan.md`](../anila_plan.md)**。
> 本檔僅保留**本 agent 樣板**自身的待辦與技術決策紀錄。

---

## 本 agent 的身份

AgenticRAG 是 **ANILA 平台的 RAG 範例 agent 樣板**（不是平台本身，也不是
runtime foundation）。它依賴的 runtime primitives 在 [`../anila-core/`](../anila-core/)；
整體平台路線圖在 [`../anila_plan.md`](../anila_plan.md)。

Agent 開發者 fork 本 repo 當作起點，改寫檢索邏輯即可做出自己的 agent。
詳情見 [`README.md`](./README.md)。

---

## 本樣板的範圍邊界

### ✅ 包含

- `api.py` — OpenAI-compat 代理 + 被動式 RAG 注入（pre-process injection）
- `index_documents.py` — 批次文件攝取 CLI（使用 `anila-core.ingestion`）
- pgvector hybrid search（語意 + 關鍵字 + RRF 融合）
- zh-TW 預設 system prompt + 來源清單格式

### ❌ 不包含（故意）

| 項目 | 理由 |
|---|---|
| Tool-driven multi-turn RAG | 需要 `anila-core.engine.QueryEngine` 整合；樣板要保持簡單，讓 fork 的人決定要不要上 |
| 多 agent coordinator | 樣板層不需要，屬於 agent 內部實作選擇 |
| 自建 session / conversation store | CSP 已經在管理對話；agent endpoint 應保持 stateless |
| 自建 auth / RBAC | 走 `CspServiceTokenMiddleware` + CSP 提供的 `X-ANILA-User-*` header |
| 自建 quota / rate-limit | 落地本地模型沒有 token / request 節流需求（平台一致決策） |

---

## 已知的 v0.3 演進方向（若有時間再做）

這些是「若要讓樣板更好 fork」的改進，**但都不是 blocking**：

1. **加入 `tests/` 的 integration case**
   - 用 `testcontainers` + pgvector 起一個臨時 DB，端到端跑一次索引 + 檢索 + 代理
   - 目前只有 unit tests（mock 住所有 I/O）

2. **拆出 `providers/` 資料夾**
   - `api.py` 內的 LLM 呼叫（`_post_chat_completion` / `_forward_stream`）
     可以抽成 `providers/chat_proxy.py`，讓 fork 的人可以換不同 LLM backend
     而不用動 handler

3. **可選的 tool-driven mode（feature flag）**
   - 設 `RAG_MODE=tool_driven` 時走 `anila-core.engine.QueryEngine` + tool loop
   - 設 `RAG_MODE=preprocess`（預設）時維持現有 pre-process 行為
   - 讓同一份樣板能示範兩種模式

4. **CSP Provider 整合**
   - 如果 `anila-core` 之後加入 `CSPPlatformProvider`（統一 LLM + Embedding
     呼叫，自動處理 s2s token），`api.py` 內手寫的 env 讀取可以收斂

---

## 技術決策紀錄

### 為什麼保持 pre-process RAG 而非 tool-driven？

- **樣板定位**是「讓人能 5 分鐘跑起來一個可註冊的 agent」
- pre-process 不依賴 LLM 的 function-calling 能力，**跨模型相容性最高**
- tool-driven 需要 LLM 支援 FC（gemma4 雖然支援，但小模型不一定），新手
  fork 後更難 debug
- 真正需要 multi-turn 的 agent 開發者會主動升級（樣板文件有提示路徑）

### 為什麼 `index_documents.py` 直接連 DB，不走 HTTP？

- 之前的 HTTP 版本依賴 AgenticRAG 內建 `/documents/*` 端點；那些端點來自
  舊 `src/anila_core/api/documents.py`，`anila-core` 抽離後，這些端點不再
  自動存在於樣板內
- 改用 `anila-core.ingestion` 直接連 pgvector 讓 CLI 變單檔可跑，也示範
  「如何在 agent 程式碼內重用 anila-core primitives」

### 為什麼不提供 `/chat` / `/agentic-chat` 自訂端點？

- CSP 要求 agent endpoint 實作 **OpenAI `/v1/chat/completions`** 介面，這樣
  Router 才能像對任何 OpenAI-compat backend 一樣轉發
- 自訂端點會讓 agent 無法直接被 CSP Router 分派，破壞「fork 即用」的樣板承諾
- 如果你 fork 後真的需要自訂端點（例如 admin UI），可以**額外**加，但
  **不要替代** `/v1/chat/completions`

---

## Release Notes

### v0.4.0（當前）— Template cleanup

- 拆分 `anila-core` 為獨立 repo，本 repo 退回純樣板角色
- `api.py` 保持 OpenAI-compat proxy + hybrid search + RRF merge
- `index_documents.py` 改為直接連 pgvector（使用 `anila-core.ingestion`）
- `Dockerfile` / `docker-compose.yml` 改為父目錄 build context（併入 anila-core）
- README 重寫為「樣板視角 + fork 指南」
- 中介層 `CspServiceTokenMiddleware` import 失敗時 **fail-fast**（不再 silent fallback）

### v0.3.x（歷史）

版本 `v0.3.0` 之前的 release notes 敘述舊 repo 的「runtime + RAG 合一」形態，
部分敘述（例如「182 tests」「/agentic-chat 端點」「src/anila_core/ 模組樹」）
與現況不符，以本檔為準。
