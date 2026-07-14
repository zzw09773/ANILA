# Gate 4 Agentic Timeline Closeout

日期：2026-07-15
定位：Gate 4 Demo Lane（唯讀 Agentic timeline ＋純 in-session cancel）

這份 handoff 對照 `docs/planning/anila-development-roadmap.md` 的 Gate 4
T1–T5 與退出條件，記錄目前實作、信任邊界、驗證證據與尚未承諾的 Gate 5
範圍。Gate 4 不提升 production readiness；本文件也不把 pending 的 PR、CI、
Fable review 或 merge 誤報為完成。

## 1. 目前 Git 脈絡

以下 SHA 是 2026-07-15 在本 worktree 實際量測結果：

| 項目 | 值 |
|---|---|
| Repository | `zzw09773/ANILA` |
| Branch | `codex/gate4-agentic-timeline` |
| Base ref | `origin/main` |
| Current `HEAD` | `afe59e1fb311e7f572382eb7bd3f894f314b9a20` |
| `origin/main` SHA | `afe59e1fb311e7f572382eb7bd3f894f314b9a20` |
| 工作樹狀態 | Gate 4 變更仍在 working tree；本 handoff 不 commit、不 push |

`HEAD` 與 `origin/main` 目前相同，Gate 4 內容尚未形成新的 commit。任何
後續 PR、CI、Fable 與 merge 證據都必須指向實際產生的 final remote head，
不能沿用本文件的 base SHA 冒充 final head。

## 2. T1–T5 實作矩陣

| ID | 狀態 | 實作與邊界 | 主要證據 |
|---|---|---|---|
| T1 producer | 完成（Demo Lane） | `packages/anila-agent/anila_agent/observability/timeline.py` 將官方 `RunHooks` 的 agent、skill/tool、retrieval lifecycle 投影成 `anila-contracts.StepEvent` named SSE；`service_wrapper.py` 接入 producer 與 cancellation teardown。摘要只送 safe projection，不送 raw tool input/output。 | Agent timeline、wire-equivalence、streaming tests；Agent suite 216 passed，另有 1 個 live case 未在本機環境選取。 |
| T2 consumer | 完成（Demo Lane） | `apps/anila-shell/src/runtime/executionReducer.js` 是單一 backend-event reducer；`sse.js`／`app.jsx` 將 `sendMessage`、edit、continue、regenerate、`sendCompare` 五條路徑接到同一套 execution callbacks。UI 不自行猜步驟完成數或狀態。 | Shell 230+ tests 與 production build 通過；reducer/SSE tests 鎖定 event-driven state。 |
| T3 trust boundary | 完成（同步 validator） | `services/csp/app/services/proxy/stream_bridge.py` 的 `StreamValidator` 只接受 `anila.step`，逐筆驗 schema、event size、rate/run budget、safe-summary 長度與 secret pattern；CSP 從 dispatch context 覆寫 task/trace/agent/session/run/classification。unknown、malformed、偽造 ID、raw reasoning、secret、event flood 不到 Shell。 | CSP full 1224 passed／31 skipped；Gate 4 focused 68 passed；wire harness 與 negative tests 通過。 |
| T4 adapter | 完成（Demo Lane） | `packages/anila-agent/examples/langchain_timeline_adapter.py` 將 LangChain callback lifecycle 映射到同一 frozen `StepEvent` fixture；不要求第三方框架改成 OpenAI Agents SDK。 | `gate4-timeline-profile-v1.json` frozen fixture；Agent adapter／wire-equivalence tests；contracts 45 passed。 |
| T5 cancellation | 完成（in-session only） | Shell cancel → CSP task cancel API → process-local registry → Router/CSP downstream stream → agent SDK run。`service_wrapper._sse_stream` 在下游取消時呼叫 `result.cancel(mode="immediate")`；CSP 只由 root/browser-facing stream author cancelled terminal。 | CSP cancellation/closure focused tests、Shell cancellation tests、Agent SDK cancellation regression；取消終態只發一次。 |

## 3. 信任邊界與取消 ownership

### 3.1 Timeline 信任邊界

- Agent 是事件的 producer，不是治理欄位的 authority。`StreamValidator` 在 CSP
  邊界重新解析 `StepEvent`，並以可信 dispatch context 綁定
  `task_id`、`trace_id`、`agent_id`、`session_id`、`run_id` 與 classification。
- Agent-originated `anila.step` 必須通過 named-event allowlist、Pydantic schema、
  size/rate/run budget 與 safe-summary secret scan。拒絕時只寫不含 payload 的
  audit log，避免把被攔截的 secret 再寫進 log。
- Router 只做受控 passthrough；Shell 只消費已驗證的 backend event。Shell reducer
  不以 token、文字內容或本地 timer 推測步驟終態。
- Agent、LangChain adapter 與 Shell 共用 `anila-contracts` 的 frozen v1 wire
  envelope；完整 replay、cursor store 與 durable idempotency 不在本 Gate。

### 3.2 Cancellation ownership

`services/csp/app/services/proxy/cancellation.py` 的
`InSessionCancellationRegistry` 只保存 process-local live-stream lease：

1. 同一 Task 的第一個 registration 視為 browser-facing/root stream，取得
   terminal owner；nested Router → CSP registration 可以收到 cancellation event，
   但不能 claim cancelled terminal。
2. 第一次 cancel 設定所有 live events。重複 cancel 在 stream teardown 完成前回傳
   `accepted=true, cancellation_in_progress`，不重新 signal；API audit 的
   `in_session_signal_delivered` 只有第一次 `accepted` 才是 `true`。
3. `claim_cancel_terminal()` 與 `finish()` 都必須帶 event 並核對 root owner，避免
   nested stream 競態搶走唯一終態。CSP 只在 owner claim 成功時輸出
   `cancelled_terminal_frame`。
4. stream teardown 最終呼叫 `complete()` 清除 process-local state，即使 durable
   closure 寫入失敗；closure ledger 仍保留 fail-closed 語意，交由後續 reconciliation
   處理，不能讓同一 Task 永遠卡在 `in_progress`。
5. `cancelled` 是一級 Task/TaskRun terminal state；closure 不把它折疊成 `failed`，
   `task.run.finished` audit 以 success 記錄「正常取消終態」。

這是純 in-session 設計。refresh、process restart、跨 worker replay、pause/approve/
resume 與 durable duplicate-event idempotency 仍屬 Gate 5。

## 4. Gate 4 退出條件矩陣

| Roadmap 退出條件 | Gate 4 結果 | 證據／限制 |
|---|---|---|
| 真實 skill → tool → retrieval 流程可在 UI 重現 | 通過 Demo Lane contract | 官方 hooks、LangChain adapter、retrieval decorator、frozen fixture 與 Shell reducer 已接線；本 closeout 不把未部署的外部 production endpoint 宣稱為 live SLO。 |
| 完成數與狀態完全來自 backend event | 通過 | `executionReducer` 與 SSE event tests；前端不再以本地猜測補終態。 |
| in-session cancel 真正停止 downstream run 且只寫一次 cancelled | 通過 | CSP registry owner race、duplicate cancel、closure regression；Agent SDK fake stream 證明 `cancel("immediate")` 僅一次且不送正常 `[DONE]`。 |
| strict schema、可信 binding、size/rate/run budget、audit negative tests | 通過 | `StreamValidator`、wire harness、偽造 ID／raw reasoning／secret／unknown／flood negative tests；CSP focused 68。 |
| 官方 agent 與 LangChain adapter 對 frozen fixture 產生等價 named SSE | 通過 | `anila-contracts` conformance 45；Agent wire-equivalence tests；contracts direct URL smoke 見下節。 |
| 完整 StreamBridge、replay、restart recovery | 不屬 Gate 4 | 明確移交 Gate 5；不可用 Gate 4 的 in-memory registry 取代 durable event store。 |

## 5. 主管獨立驗證證據

以下是主管 closeout validation ledger 的結果；本節保留 skip 的來源，避免把
環境分流誤寫成 quarantine 或 Gate 4 blocker：

| 範圍 | 結果 |
|---|---|
| CSP full | `1224 passed, 31 skipped` |
| CSP Gate 4 focused | `68 passed` |
| Shell | `230+ tests passed`，production build passed |
| `packages/anila-agent` | `216 passed`；`1` live case deselected／未設 live endpoint |
| `packages/anila-contracts` | `45 passed` |
| `packages/anila-core` | `808 passed, 11 skipped` |
| Router | `25 passed` |
| Governance | `19 passed` |
| Quality gates | Ruff 與 `git diff --check` passed |
| Capability freeze | `25 surfaces / 372 entries` |

31 個 CSP skip 與 core 的 11 個 skip 是既有 Gate 2/3 PostgreSQL integration
environment 分流，不是 Gate 4 新增的 PG requirement。Gate 4 的同步 trust-boundary、
wire、cancel 與 adapter tests 不依賴新增 PostgreSQL acceptance；若後續要補跑 PG，
那是既有 Gate 2/3 integration evidence 的環境工作，不應改寫 Gate 4 的退出條件。

### 5.1 Container／supply-chain smoke

- Docker image：`anila-agent:gate4-final`
- image id：`sha256:46916c7c...`
- runtime user：`anila`，UID/GID `10001`
- internal contracts provenance：`file:///src/anila-contracts` direct URL；未讓公開
  package index 以同名 distribution 取代 monorepo contract
- container wire smoke：容器內可解析 `anila.step` 的 `cancelled` event

## 6. Supervisor 找出的問題與修正

以下項目是 closeout 前由主管驗證找出、並已補回歸的問題，不代表範圍擴張：

1. **Duplicate cancel**：第二次 cancel 原先可能觸發 browser abort fallback；改為
   `CancellationResult` 的 `ACCEPTED/IN_PROGRESS/NO_ACTIVE_STREAM` 明確語意，duplicate
   accepted 但不重送 signal，Shell 保持 socket 等 trusted terminal。
2. **Outer/root ownership race**：nested Router stream 可能先 claim；root registration
   現固定為唯一 terminal owner，`claim` 與 `finish` 同時核對 owner。
3. **Cleanup leak**：closure persistence failure 原先可能殘留
   `cancel_requested/owner/claim`；stream final teardown 現無條件釋放 process-local lease，
   durable ledger 仍 fail-closed。
4. **Dependency confusion**：agent developer install 原先可能讓公開 index 滿足
   `anila-contracts`；Makefile 改為 `pip install -e ../anila-contracts -e '.[dev]'`，
   並加靜態 packaging regression。
5. **Test coverage regression**：保留並恢復原有 Shell Task 建立、header 與降級案例，
   另補 duplicate cancellation / abort fallback tests。
6. **SDK cancellation coverage**：補 `_sse_stream` CancelledError regression，證明 fake
   SDK run 僅收到一次 `cancel(mode="immediate")`，且取消路徑不送正常 finish／`[DONE]`。
7. **Cancelled closure semantics**：`cancelled` 保持一級 terminal state，Task/TaskRun
   ledger 與 trusted cancelled event 一致，audit status 為 success。

## 7. 未完成項與後續 gate

| 項目 | 狀態 |
|---|---|
| Gate 5 durable replay / idempotency / restart recovery | **未做，明確 pending** |
| durable pause／approve／resume／EventStore | **未做，明確 pending** |
| PR 建立與 remote final-head 證據 | **pending** |
| CI required checks on final remote head | **pending** |
| exact `claude-fable-5` final review | **pending** |
| review threads／PR closeout | **pending** |
| merge to `main` | **pending** |

在上述 pending 項目完成前，不得宣稱 Gate 4 已 merge，也不得進入 Gate 5。下一位
接手者應先以 final commit 重新跑 branch/base、working-tree、CI 與 Fable provenance，
再更新本 handoff，而不是把目前 `afe59e1...` base SHA 當作 Gate 4 final release。

## 8. 基本檢查

本文件新增後執行：

```powershell
git diff --check
```

結果：passed。未 commit、未 push。
