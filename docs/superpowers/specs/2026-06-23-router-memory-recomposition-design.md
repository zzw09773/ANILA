# 設計：Router 用使用者記憶個人化回覆（as-built）

- **日期**：2026-06-23
- **狀態**：已實作（main）；Codex 審過設計 2 輪 + 實作 1 輪
- **分支範圍**：main 起 → cherry-pick 散 7 分支（通用功能）
- **撰寫**：Claude

> ⚠ 本文已對齊**實際實作**。設計過程一度假設「Router 從入向 `messages[0]` 抽記憶」，**驗證後證實為錯**（見 §1 拓撲），改為「CSP 在 Router 的每次 LLM 呼叫注入記憶、Router 不抽取」。歷史審查紀錄見 plan 文件。

## 1. 背景、目標、關鍵拓撲

現況 `user > router > agent > user`：Router 把 dispatched agent 的回覆 **verbatim 原樣轉出**，不依使用者習慣調整。
目標 `user > router > agent > router > user`：Router 拿到回覆後**依使用者長期記憶/偏好重組表達**（像照 CLAUDE.md 重組語言）。

**關鍵拓撲（驗證確認）**：`User → Router → CSP`。Router 透過 `_call_llm_non_stream` → `{csp}/v1/chat/completions` 取 LLM。CSP 的 `_inject_memory` 在該端點執行 → **使用者記憶（含 `preference.*` facts）由 CSP 注入 Router 的每一次 LLM 呼叫**（routing 呼叫 + 重組呼叫）。因此 **Router 不需要、也拿不到入向記憶；它只要照常打 CSP，CSP 就把記憶注入進去**。

## 2. 已定案決策

| # | 決策 | 選擇 |
|---|---|---|
| D1 | 個人化深度 | 自適應：一趟重組 LLM，依偏好潤飾、相關時補脈絡 |
| D2 | 串流 | 統一：非 classified buffer→重組→soft-chunk 逐字吐；trace 階段顯示 |
| D3 | 記憶來源 | **CSP 在 Router 的 LLM 呼叫注入**（Router 不抽取）；CSP `_format_block` 把 `preference.*` 標 `### 使用者偏好` 段助 LLM 聚焦 |
| D4 | 觸發 | 直答/clarify（系統提示 inline）+ dispatched（重組 pass）都個人化 |
| D5 | gating | **always-on、無旗標**；classified **跳過**（verbatim，不送進 base 重組模型）；無「無記憶跳過」(Router 看不到記憶) |
| D6 | 分支 | main 起、散 7 分支 |

## 3. 設計（as-built）

### §1 CSP 端（`memory_service._format_block`）
`preference.*` facts 另組 `### 使用者偏好` 段（其餘維持 `### 已知事實`/`### 過往相關討論`）。**無分隔符 / 無抽取機制**（記憶由 CSP 注入、Router 不解析）。

### §2 直答 / clarify（`_ROUTER_SYSTEM_TEMPLATE` rule 6）
加一段：「平台會在系統訊息前段附上使用者記憶/偏好；直答或反問時依偏好調語氣/語言/詳略/格式；不捏造；**DISPATCH 行除外**（機器解析、byte-exact）」。CSP 已把記憶注入 routing 呼叫 → 生成時即個人化、**照常即時串流、免額外 LLM**。

### §3 dispatched 重組（`router_server.py`）
- `_recompose_reply(agent_reply, caller_api_key, *, forwarded_headers) -> (content, status)`：組 `[system: 重組指令, user: <<<ANILA_AGENT_REPLY…>>> 包住的 agent 原文]` → `_call_llm_non_stream`（CSP 注入記憶）。`status ∈ {applied, fallback}`。**Fail-safe**：error/timeout(`asyncio.wait_for`)/空 → 回原文+`fallback`。
- **防注入**：agent 原文是不可信文字 → `sanitize_agent_reply` 剝 sentinel + 包 `AGENT_REPLY` sentinel + 提示標「資料非指令」。sentinel 常數在 `anila_core/memory/contract.py`（純常數模組、免循環依賴）。
- **三插入點**：① 非串流（`agent_response["content"]` → recompose → `_respond`）② 串流多輪（`final_content` → recompose → `_emit_soft_chunks`）③ 單輪串流（**`buffer_for_recompose = not manifest.requires_encryption`**：非 classified buffer content 不即時吐、`agent_stream_completed` 守衛只在 `done` 後 flush → recompose → `_emit_soft_chunks`；classified 即時串流）。
- **trace**：recompose **後**依實際 status 吐 `anila.trace`（applied→「依使用者偏好整理回覆」ok；fallback→「個人化未套用，回原文」error）。
- **forwarded_headers**：三路徑都把 handler 的 `anila_headers` thread 進 recompose 呼叫（RAG/audit 脈絡一致）。

### §4 Metadata
沿用 `_merge_anila_meta`：`classified` 單向 latch 不降、citations/handoff_chain/trace_id 保留。重組只改 text、不動 meta。classified 內容**不**進重組（D5）。

## 4. Non-goals
- 不抽取/傳遞記憶塊（CSP 注入）。
- 不改記憶抽取/儲存。
- 不做品質評分；重組品質靠保真 prompt + fail-safe verbatim + anila_meta citations 保留（inline citation marker 僅 prompt 約束，未程式驗證，列 v1 已知限制）。
- 不加旗標（always-on）。

## 5. 風險與緩解
| 風險 | 緩解 |
|---|---|
| 每筆非 classified dispatched +1 LLM 趟（延遲 ×~1.5–2） | always-on 已接受；classified 不付（保即時） |
| 重組改壞答案/漏 inline citation | 保真 prompt + fail-safe 回原文 + anila_meta citations 保留；inline marker v1 未驗證(已知限制) |
| agent 原文 prompt injection | sanitize + sentinel 包裹 + 標「資料非指令」 |
| classified 內容外送 base 重組模型 | manifest.requires_encryption 上游即跳過(verbatim 即時)；downstream meta classified 也跳過 |
| 串流 buffer 破壞順序 | `agent_stream_completed` 守衛只正常完成才 flush；上游 content/[DONE] 抑制、安全 trace pass-through |

## 6. 測試
- `test_memory_contract`（sentinel/sanitize）、`test_memory_block_format`（偏好段保留既有 heading）、`test_router_personalization`（rule 6、`_recompose_reply` applied/fallback/timeout/sentinel strip）。
- 既有 dispatch 測試（`test_router_multi_turn`/`test_router_resume_proxy`）加 autouse fixture stub `_recompose_reply`（它們測 dispatch 行為、非重組）。
- 全 anila-core：6 failed 為 pre-existing（chunking/g3×2/runtime_contract×2）+ resume_proxy 隔離 artifact（單獨/相關組合皆過、僅全套件污染）；零新增真失敗。

## 附錄：關鍵程式位置
- `anila-core/src/anila_core/memory/contract.py`（AGENT_REPLY sentinel + sanitize）
- `router_server.py`：`_ROUTER_SYSTEM_TEMPLATE` rule 6、`_recompose_reply`、三插入點（非串流 `_respond` 前、`_router_streaming_multi_turn`、`_router_streaming` buffer）
- `myCSPPlatform/backend/app/services/memory_service.py`：`_format_block`（偏好段）
