# P2-15 Backlog — 未實作的 4 個 misc patterns

> 對應 `docs/agent-framework/claude-code-src-deep-dive.md` §4.23 — 「5+ 額外
> untapped patterns」清單共 9 個。本次 P2-15 已實作 5 個 high-value pattern,
> 以下 4 個延後到後續 sprint。

---

## 本次已實作(5 個)

| Pattern | 模組 | 測試 |
|---|---|---|
| `maxResultSizeChars` + tool result storage | `anila_agent/tools/result_storage.py` | `tests/test_tool_result_storage.py` (15) |
| `autoCompactBoundary` 訊息 | `anila_agent/memory/compact_boundary.py` | `tests/test_compact_boundary.py` (14) |
| `stripSignatureBlocks` | `anila_agent/memory/signature_blocks.py` | `tests/test_signature_blocks.py` (15) |
| `getInMemoryErrors()` ring buffer | `anila_agent/utils/error_buffer.py` | `tests/test_error_buffer.py` (16) |
| `logEvent(...)` 結構化分析事件 | `anila_agent/utils/analytics.py` | `tests/test_analytics.py` (17) |

---

## 延後 backlog(4 個)

### 1. `pendingToolUseSummary` — tool sequence 摘要

**來源**:`src/services/toolUseSummary/toolUseSummaryGenerator.ts`
**預估**:1d
**延後理由**:
- 跟既有 **P1-12 `MicroCompactor` / `LlmSummaryCompactor`** 功能重疊(都是用
  small model 摘要 tool 互動歷史)。
- 真正獨立的功能 surface 是「tool sequence 結束時主動摘要」而非 compaction
  觸發,需要一個 `tool_sequence_complete` lifecycle hook 才有 attach 點。
- 待 P3 增加新 hook event 後再回頭做。

**最低 dependency**:`P1-12 compactor` + 新 lifecycle hook `tool_sequence_end`。

---

### 2. `fileHistorySnapshot` — cwd 快照供 `/rewind` 用

**來源**:`src/utils/fileHistory.ts`
**預估**:1.5d
**延後理由**:
- claude-code 是 desktop CLI,直接掌控 cwd,snapshot 整個工作目錄合理。
- **anila-agent 是 headless service**,沒有「使用者 cwd」概念 —— 檔案 I/O
  通常經由 platform / S3 / DB,直接快照 fs 等於白做。
- 真要做應該是「**Session-scoped working file table**」(類似 git 的 staging
  area),不是 fs snapshot;這是另一個 P3 等級的功能設計題,不適合塞進 P2-15。

**最低 dependency**:確認 anila 平台是否提供 session-scoped working area;
重新設計 snapshot 目標(file path → blob hash 表)。

---

### 3. `bashCommandHelpers` + heredoc / shellQuote — 安全 parse Bash command

**來源**:`src/utils/bash/*.ts`
**預估**:2d(本清單中最大宗)
**延後理由**:
- claude-code 自己跑 Bash tool(spawn child process),需要解析 heredoc /
  pipe / 反引號等 shell grammar。
- **anila-agent 不直接執行 Bash** —— ANILA platform 提供 Bash tool API,
  agent 把 command 字串送出去,parsing 與 sandbox 都在平台側。
- 若未來真的要在 agent 側做 pre-flight validation(例如解析 command 拒絕
  危險 sub-expression),才需要這層。
- 工作量大且現在沒有實際 caller。

**最低 dependency**:確認 anila 是否需要 client-side Bash command validation
(目前由 `permission_grammar` / `policy` 在「字面層級」攔截足夠)。

---

### 4. `AbortController` with reason — 結構化中斷理由

**來源**:整個 `src/Tool.ts` 的 abortController 信號傳遞
**預估**:1d
**延後理由**:
- 部分已做:`AnilaRunner.RunSummary` 已有 `aborted: bool` + `abort_reason: str`
  欄位(`core/runner.py:36-37, 99-100`)。
- 缺的是「結構化 reason enum + 跨 hook 一致傳遞」:目前 `abort_reason` 是
  raw string(``str(e)``),callsite 要分辨「user cancelled」/「max_budget」/
  「stop_hook」要 string match,太脆弱。
- 完整做法需要:
  1. 定義 `AbortReason` enum(user_cancel / budget_exceeded / stop_hook /
     guardrail_tripwire / 其他)。
  2. 改造 `AnilaRunner` / `hook.fire(...)` / `cost_tracker` / `policy_limits`
     都統一用 enum。
  3. 對應 `RunSummary` 加 `abort_reason_code: AbortReason | None` 欄位
     (保留 `abort_reason: str` 向後相容)。
- 屬於跨模組 refactor,適合單獨 P3 sprint 處理。

**最低 dependency**:無新模組;refactor 既有 runner + hook + cost_tracker。

---

## 總結

| 類別 | 數量 | 說明 |
|---|---|---|
| 本次實作 | 5 | high-value、低耦合、無新依賴 |
| 延後 — 重疊既有 | 1 | pendingToolUseSummary 跟 P1-12 重疊 |
| 延後 — 平台不對齊 | 2 | fileHistorySnapshot / bashCommandHelpers 不適用 headless service |
| 延後 — 既有部分覆蓋 | 1 | AbortController-with-reason 已 partial(`abort_reason: str`)|

四個 backlog 都有「需要做什麼前置 work」與「最低 dependency」可追,建議在 P3
裡視 ANILA platform 整合進度逐項評估是否升級成正式 task。
