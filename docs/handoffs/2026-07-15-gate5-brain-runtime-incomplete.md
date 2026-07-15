# Gate 5 Brain Runtime handoff（PRE-REVIEW CHECKPOINT）

更新時間：2026-07-16 00:59（UTC+8）

## 目前定位

- Worktree：`C:\Users\USER\.codex\worktrees\ANILA-gate5`
- Branch：`codex/gate5-brain-runtime`
- Base：`prod-intranet-card`
- Draft PR：[#31](https://github.com/zzw09773/ANILA/pull/31)
- Remote：`origin=https://github.com/zzw09773/ANILA.git`
- 文件更新開始時的 committed／remote checkpoint：`eb3ab37710aeec7e8efa4fb88b6db2d61b3d9535`
- 目前 working tree 含尚未 commit 的 Gate 5 收尾變更；`eb3ab37` 的 required checks
  全綠只代表舊 remote checkpoint，不代表尚未 push 的 working tree 已通過 CI。
- 本文件不預填下一個 commit 或 final PR head 的未知 SHA。commit／push 後必須以
  `git rev-parse HEAD` 與 `gh pr view 31 --json headRefOid,statusCheckRollup` read-back。

Gate 5 已完成主要實作與完整 Docker 證據，但目前仍是 **pre-review checkpoint**。
Durable event cap、Gate 5 Core 新增／修改段落的型別修正與最終受影響驗證均已完成；
目前剩餘的是 commit/push、新 head CI、Sol review、Claude Final Review Gate 與
thread resolution。在這些關卡全部通過前，不得把 Gate 5 宣告 closed 或 merge。

## 已完成實作

### R1–R3：contracts、readiness、formal routing

- Contracts v2、Agent readiness、signed ExecutionGrant 與 fail-closed admission 已接線。
- Router 已使用 structured `ExecutionRuntime`：`RequestContextBuilder` →
  `CapabilityFilter` → `DecisionEngine` → `PolicyGate`，並保留 dispatch/session/streaming
  契約。
- R3 deterministic adapter 已接到 R6 runner；GitHub required job
  `Gate 5 / routing runtime exit gate (required)` 已實際執行，不再是
  `SKIPPED`／incomplete。
- Frozen 160-case runtime 結果：`route_top1=60/60=1.0`、
  `false_dispatch=0/90`、`policy_bypass=0/20`。

### R4：durable stream、terminal authority、provenance

- CSP-owned `SessionEventStore` 已有 InMemory conformance store 與 SQLAlchemy durable
  adapter，server cursor、binding、source order、idempotency 與 PostgreSQL race contracts
  均已建立。
- 普通 Agent wire `completed/failed/cancelled` 不再能自行鎖住 run；只有
  CSP-internal `authoritative_terminal=True` 可建立 terminal latch。Authority bit 是
  idempotency identity 的一部分，不能把普通 event 靜默升級成 terminal。
- Durable ordinary-event cap 預設固定為 500；InMemory conformance store 依 store-owned
  `next_cursor` 跨 bridge instance 維持 budget，SQL store 則在 append transaction 內依
  locked durable `next_cursor` 執行，跨 DB session/process restart 不會重置。超限回固定
  typed `409 EVENT_RUN_BUDGET_EXCEEDED`；duplicate retry 不重複消耗 budget，CSP
  authoritative terminal 可在 cap 滿後 bypass 並安全關閉 run。
- CSP→Router provenance 使用 signed context token；Router 從 verified claims 建立
  formal context，不信任 caller 可自行偽造的 raw identity headers。
- Usage writer 的同步 DB commit 已移出 ASGI event loop；healthcheck 具 timeout，避免
  DB lock 或 readiness wedge 卡住整個 CSP。

### R5：official Agent Silver 與 restart-safe resume

- `ResumeAuthority`／`ResumeAttempt` durable models、migration、renewed grant 與 formal
  resume path 已落地；Router restart 後可從 CSP authority 恢復，不依賴程序內 cache。
- Official `anila-agent` 有 non-root Docker target、Silver compose profile、persistent
  runstate volume、正式 service token boundary 與 restart/idempotency tests。
- Agent RunState 只保存可安全序列化的 SDK state；fresh runtime context/retriever 在
  resume 時重新綁定，不把 credential、token 或 runtime object 寫入 durable state。
- OpenAI SDK 相容版本已固定，避免 SDK schema 漂移破壞 HITL state restore。

### R6/R7：routing quality、model governance、deployment posture

- Frozen routing dataset、adapter mutation guards、required runtime exit job 均已接上。
- Model callsite inventory、disabled-template verifier、CSP gateway admission/receipts、
  internal model network、compose/image/backup posture 與 capability-freeze checks 已接線。
- Production governance profile 仍是 disabled template；沒有 production approver、
  signature、artifact/deployment digest 或法務裁決。FLUX.2-dev 法務界線未放寬。

## Docker／HTTP 端到端證據

Disposable `gate5-silver-e2e` fresh-host 等價流程已實跑 PASS：

1. fresh isolated Docker project/volumes 啟動 CSP、Router、official Agent 與 E2E model；
2. formal dispatch 進入 `BLOCKED`；
3. restart/recreate CSP、Router、Agent；
4. 透過正式 Router/CSP path approve/resume；
5. replay 回傳既有 durable result，沒有重複 side effect，terminal ledger 維持
   exactly-once；
6. Full Trace 以正式 HTTP POST 寫入 CSP，再從 CSP trace API read-back；trace/task/run、
   classification、producer、parent span 與 content hash binding 全部通過。

Harness 的成功終訊為：

```text
PASS: Gate 5 Silver official anila-agent dispatch -> BLOCKED -> restart -> resume -> replay; CSP ledger and Full Trace HTTP readback verified
```

## 已觀察驗證結果

下列是目前已完成的 validation batch；數字是各 suite 的實際結果，套件間可能重疊，
不可相加成單一總數：

| 範圍 | 結果 |
| --- | --- |
| CSP full（event-cap 變更後） | `1476 passed / 40 skipped` |
| anila-core full | `902 passed / 11 skipped` |
| anila-core Gate 5 focused | `68 passed`；Ruff PASS |
| official anila-agent | `256 passed / 1 skipped` |
| anila-contracts | `65 passed` |
| anila-security | `44 passed` |
| deployment + CI + policy | `223 passed / 2 skipped` |
| fresh PostgreSQL migration + R4/R5 | `14 passed` |
| Agent strict mypy | PASS |
| changed-file Ruff | PASS |
| Docker restart/resume/replay + Full Trace | PASS |

品質 baseline 必須如實保留：

- Formatter scan：57 個 changed Python files 中，50 個 `would reformat`。這是現有
  branch/baseline 狀態，不能冒充 formatter 全綠；也不要在收尾時無界限重排整個
  monorepo。
- anila-core global mypy：本機此次仍看到 101 個 historical baseline errors，其中
  `router_server.py` 尚有 9 個既有泛型 `dict` errors。已修正的是 **Gate 5 新增／修改
  段落的具體型別錯誤**；完整 mypy 仍由既有 baseline 阻擋，不能寫成 Gate 5 targeted
  mypy 全檔 0，也不能宣稱 global mypy 已全綠。

## 剩餘 publication／review gates

1. Review 完整 diff 與 secret scan，逐檔 stage Gate 5 變更後 commit；不得使用
   `git add -A`／`git add .`。
2. Push 後等待新 head 的全部 required CI；舊 `eb3ab37` 綠燈不可沿用。
3. CI 全綠後依序進行 Sol 唯讀 review、修正所有有效 findings並重跑 affected
   validation。
4. 執行 Claude Fable5 max Final Review Gate；修正 findings、重跑 CI 並 resolve 所有
   review threads。
5. 全部通過才可把 Draft 改 ready／merge。

## Exact resume commands

先讀回工作樹、PR 與 remote provenance：

```powershell
cd C:\Users\USER\.codex\worktrees\ANILA-gate5
git status --short --branch
git rev-parse HEAD
git log -5 --oneline --decorate
git diff --stat
git diff --check
gh pr view 31 --json number,url,state,isDraft,headRefName,baseRefName,headRefOid,mergeable,reviewDecision,statusCheckRollup
```

重跑 R3/R6 required routing contract：

```powershell
$env:PYTHONPATH='.'
python -m pytest infra/ci/tests/test_gate5_r3_eval_adapter.py infra/ci/tests/test_gate5_routing_contract.py -q
python infra/ci/run_gate5_routing_contract.py --adapter infra.ci.gate5_r3_eval_adapter:build_adapter --runner-commit $(git rev-parse HEAD)
```

重跑 Gate 5 focused event/resume tests：

```powershell
$env:PYTHONPATH='packages/anila-contracts/src;packages/anila-security/src;packages/anila-core/src;services/csp'
python -m pytest `
  services/csp/tests/test_gate5_r3_dispatch_contract.py `
  services/csp/tests/test_gate5_r4_stream_bridge.py `
  services/csp/tests/test_gate5_r4_durable_session_event_store.py `
  services/csp/tests/test_gate5_r4_session_event_store_pg.py `
  services/csp/tests/test_gate5_r5_durable_resume_authority.py -q
```

真 PostgreSQL 必須使用隔離測試 DB，不得指向 production：

```powershell
$env:TEST_POSTGRES_URL='<isolated-test-postgres-url>'
$env:DATABASE_URL=$env:TEST_POSTGRES_URL
$env:PYTHONPATH='packages/anila-contracts/src;packages/anila-security/src;packages/anila-core/src;services/csp'
python -m pytest `
  services/csp/tests/test_gate5_r4_session_event_store_pg.py `
  services/csp/tests/test_gate5_r5_durable_resume_authority.py -q -m integration
```

重跑 disposable Docker Silver E2E（預設成功或失敗都會清理 project/volumes；除錯時才
明確設定 `GATE5_E2E_KEEP=1`）：

```powershell
bash infra/deployment/scripts/gate5-silver-e2e.sh
```

Commit/push 前後 read-back：

```powershell
git diff --check
git status --short --branch
git diff --name-only
# 僅逐檔 stage 本 Gate 變更；不要 git add -A / git add .
git rev-parse HEAD
git push origin codex/gate5-brain-runtime
gh pr checks 31 --watch
gh pr view 31 --json headRefOid,statusCheckRollup,isDraft,mergeable,reviewDecision
```

## Review sequence

1. 新 head required CI 全綠。
2. Sol 唯讀 review；修完所有有效 findings並重跑 affected validation。
3. 執行使用者指定的 Claude Final Review Gate：

```powershell
claude -p "Run a read-only final review of ANILA PR #31 at the exact current head. Review correctness, security, restart durability, event idempotency/budget, signed provenance, deployment posture, test gaps, and Gate 5 evidence. Do not edit files. Return file:line findings and an explicit Approve or Request changes verdict." --model claude-fable-5 --effort max --output-format json
```

`claude -p` 的 caller timeout 不代表程序已掛掉；若 timeout，先檢查 process/session 與
持續輸出，不得只因等待時間較長就殺掉。Fable5 是首選；只有 Fable5 明確不可用時，
才可把下列 Opus 4.8 命令當成**單獨替代的 fallback**，並在 review evidence 記錄替代
原因。不得把 Fable5 與 Opus 4.8 混稱為同一次 review 或合併兩者 verdict：

```powershell
claude -p "Run a read-only fallback final review of ANILA PR #31 at the exact current head. Review correctness, security, restart durability, event idempotency/budget, signed provenance, deployment posture, test gaps, and Gate 5 evidence. Do not edit files. Return file:line findings and an explicit Approve or Request changes verdict." --model opus --effort max --output-format json
```

4. 修完 Claude findings、重跑 affected validation/CI、resolve所有 review threads。
5. Sol、Claude Final Review Gate、本地驗證、CI、mergeability與 threads 全部通過後，
   才能 merge Gate 5；Gate 5 merge 後才可開始 Gate 6。

## Gate 6 是外部 production blocker

Gate 5 完成只允許進入 Gate 6，不代表 production Go。至少下列 Gate 6 條件不能由
AI、單一工程師或本 PR 自行滿足：

- **P0 signed acceptance profile**：production topology、enabled features、資料上限、
  SLO/RTO/RPO、workflow matrix、inference callsite inventory與簽核角色必須先凍結並
  由權責人員簽章，不能看完結果再調低門檻。
- **P3 七日觀測**：依 signed load profile 連續觀測至少 7 日，且無未處置
  Sev-1／Sev-2。
- **P6 獨立人員覆核**：必須由不同於修補者的具名 reviewer 執行；AI 不能代替獨立
  人員簽署。
- **P7 法務／授權**：每個 enabled model、dataset與第三方元件都要有軍方環境適用的
  書面裁決；未核准者必須從 profile、bundle、registry與 routing capability 排除。
- **P8 實體卡矩陣**：production-equivalent OS/browser/HiPKI/reader/card/PKI/CRL、撤銷、
  race、refresh reuse、`jti` 與 break-glass 必須用實體環境完成證據。
- **五方人員簽核**：P0–P9 證據最終須由 system owner、data owner、PKI owner、資安、
  維運五方的人員簽核。

任一條缺 owner、缺證據或未達門檻，結論一律是 No-Go。AI 可以協助整理證據，不能
宣告 production Go。
