# Gate 3 已合併／Gate 4 起跑 Handoff

日期：2026-07-14

這份文件是進公司後的直接續作入口。Gate 3 已完成 review、CI、merge 與本機同步；
不需要重做 Gate 2／Gate 3，也不得把 Gate 4 Demo Lane 誤報為 production readiness。

## 1. 權威狀態

| 項目 | 值 |
|---|---|
| Repository | `zzw09773/ANILA` |
| Gate 3 PR | [#28](https://github.com/zzw09773/ANILA/pull/28) |
| Base | `main@f647219815f9e1514ade3c2d3645fea656f06d51`（Gate 2 merge） |
| Gate 3 final head | `9c8826b4c0170b31afebda5ad2652087a058a56c` |
| Gate 3 merge commit | `61d4ed320ffd417a660c9b5d77fc753133d8755f` |
| Merge time | `2026-07-14T14:47:12Z` |
| Local SSOT | `D:\ANILA` 已切到 `main` 並 fast-forward 至 merge commit |
| 完整 closeout | `docs/handoffs/2026-07-14-gate3-data-artifact-durability.md` |

## 2. Gate 3 已完成範圍

- ingestion upload／reprocess 使用 transactional outbox；Redis failure、crash replay、
  deterministic Arq identity 與 terminal starvation 均有回歸測試。
- worker 有 PostgreSQL lease／fence／heartbeat／retry／reaper／DLQ；Redis 使用 AOF。
- ingestion generation immutable、active pointer 原子切換，並綁 embedding fingerprint／dimension。
- Studio 五條 pipeline 使用 durable Redis envelope、lease/fence、restart supervisor。
- ArtifactVersion blob 由 CSP content-addressed immutable store 作唯一 SSOT；下載／匯出重新驗
  owner、Task、snapshot/collection、classification、compartment、version 與 revocation。
- ANILALM 不再持久保存 artifact metadata，改讀 CSP。
- retention/archive/legal-hold/reaper 已涵蓋 FORCE-RLS image/chunk/relation rows、磁碟 bytes、
  counter reconciliation 與 race/idempotency。
- production backup profile、加密/off-host policy、prepared restore automation/runbook 已完成。
  Gate 6 才做異機／破壞性 DR drill。
- 正式 application images 以 UID/GID 10001 執行，shared volumes 已做雙向 read/write/delete smoke。

## 3. 最終驗證證據

### GitHub final head `9c8826b`

- 17 checks 全綠。
- Gate 1 workflow run：`29340858299`。
- Gate 0 workflow run：`29340859509`。
- CSP full suite：7m22s，pass。
- PostgreSQL RLS：2m40s，pass。
- Deployment contracts：28s，pass。
- Python security、capability governance、三個前端與所有 backend jobs 全綠。

### 本機

- CSP：`1209 passed, 31 skipped`；31 項為 PostgreSQL 分流，已用 disposable
  `pgvector/pgvector:pg16` 補跑。
- outbox＋retention focused：`26 passed`。
- Gate 3 retention/upload/FORCE-RLS：`3 passed`，使用 `csp_app` 非 superuser。
- production backup automation：`16 passed, 1 skipped`。
- deployment contracts：`135 passed, 2 skipped`。
- 真 PostgreSQL CI 等級矩陣 exit 0：core RLS、Gate 2 reconciliation/runtime/sealing/
  memory/atomicity/auth、Gate 3 retention/generation 全綠。
- Ruff、test governance（0 xfail／0 quarantine）、capability freeze（25 surfaces／372
  entries）、current-tree card/key material scan（0 finding）全綠。

### Docker 清理

- disposable PostgreSQL／Redis、`anila-platform` 與 `anila-platform-dev` 測試資源已移除。
- 沒有保留所謂「正式」volume；目前環境依 owner 決策全是可丟棄的開發／測試狀態。
- 未觸碰非 ANILA 的 Docker 資源。

## 4. Review closeout

- Fable full review：`f647219..691097c`，實際 model 僅 `claude-fable-5`
  （127 model messages），F1 retention RLS 與 F2 outbox starvation 均關閉，verdict
  **Approve**。
- Fable final incremental review：`691097c..9c8826b`，實際 model 僅
  `claude-fable-5`（53 model messages），backup OSError fail-closed 修正無新 blocker，
  verdict **Approve**。
- GitHub Gemini inline thread已修正、回覆並 resolve：backup external command 無法啟動時，
  `OSError/FileNotFoundError` 會轉成保留 cause 的 `BackupAutomationError`。
- PR closeout comment：<https://github.com/zzw09773/ANILA/pull/28#issuecomment-4970520758>。

## 5. 非阻擋殘餘

以下不回退 Gate 3，但後續改到相鄰程式時應處理：

1. outbox `pending` inconsistent 分支目前仍採短 backoff；現有寫入路徑不可形成永久 poison
   row。未來新增 ingestion cancel／人工狀態修改時，要同步改成 bounded drain 或明確終態。
2. retention rowcount/residual 是同一 RLS 視野內的檢查，不能單獨偵測 GUC 完全漏設；真
   PostgreSQL mutation regression 是守住 `_scope_collection_rls` 的必要防線，不得降級為
   SQLite-only。
3. backup 主流程已統一 OSError；部分 restore-side 仍直接呼叫 subprocess。它們仍 non-zero
   fail-closed，但錯誤碼／診斷未完全一致，可在 restore automation 後續維護時收斂。
4. artifact store 的 parent-directory fsync／orphan temp GC、Studio envelope key rotation
   runbook、legacy `replace_document_chunks` generation contract 等屬後續 hardening。
5. 實體卡完整矩陣、異機 DR restore、PKI/CRL freshness、實體 ingress/egress、SLO、signed
   acceptance profile 與 signed release artifact 仍是 Gate 6 blocker。Gate 3 merge 不代表
   機密 production Go。

## 6. 下一步：Gate 4 Demo Lane

Gate 4 只做「唯讀 Agentic timeline＋純 in-session cancel」，不做 durable pause／approve／
resume／replay／restart recovery；後者全部屬 Gate 5。

建議依安全邊界與依賴順序拆分：

1. **T3 foundation**：先建立薄型 `StreamValidator`／`BridgeCore`。strict event-name
   allowlist、逐型別 schema、可信 task/trace/agent/session binding、單事件大小、rate/run
   budget、safe-summary secret scan 與 audit negative tests 必須先成立。
2. **T1 producer**：讓官方 `anila-agent` RunHooks 真正發 tool／skill／retrieval events；沿用
   `anila-contracts.StepEvent`，不得另造第二套 wire schema。
3. **T2 consumer**：Shell 建單一 execution reducer，五條路徑 `sendMessage`、
   `handleEditUser`、`continueMessage`、`regenerateMessage`、`sendCompare` 全部共用 callbacks。
4. **T5 cancel**：Shell → CSP/Router → Agent run 真正傳遞取消，downstream 停止且只寫一次
   cancelled 終態；不做 restart recovery。
5. **T4 adapter/E2E**：官方 LangChain adapter 與 `anila-agent` 對 frozen event fixture 產生
   等價 named SSE；用真實 skill → tool → retrieval 流程驗 UI timeline。

第一支分支建議：`codex/gate4-stream-validator`。先不要從 UI 動畫或 reducer 開始；未驗證的
第三方 event 不得先送進 Shell。

## 7. 進公司後的 resume commands

```powershell
Set-Location D:\ANILA
git status --short --branch
git fetch --all --prune
git switch main
git pull --ff-only origin main
git rev-parse HEAD
# 預期包含 Gate 3 merge commit 61d4ed320ffd417a660c9b5d77fc753133d8755f

git switch -c codex/gate4-stream-validator
```

動手前重新量測 roadmap 所列路徑；目前權威入口是：

- `packages/anila-contracts/src/anila_contracts/events.py`
- `packages/anila-core/src/anila_core/api/events.py`
- `packages/anila-core/src/anila_core/api/router_server.py`
- `apps/anila-shell/src/runtime/sse.js`
- `apps/anila-shell/src/app.jsx`

Gate 4 實作完成後仍須以 `claude -p --model claude-fable-5 --effort max` 審最終 remote
head；Gate 4 未通過前不得進 Gate 5。
