# Gate 2 governance ledger 接手紀錄（2026-07-13）

> 狀態：**WIP／Request changes／不可 merge／不可宣告 Gate 2 完成**
>
> 用途：讓維護者在公司環境從遠端 branch 接續開發。這份文件記錄的是
> checkpoint，不是 Gate 2 closeout 或正式驗收證據。

## 1. 接手位置

- Repository：`zzw09773/ANILA`
- Branch：`feat/gate2-governance-ledger`
- Gate 0/1 收斂基準：`369f315df12059433fa47e9d1005c0e779365197`
- 本輪已提交的最後一筆完整 commit：
  `94f70d41dc746d366e3ededb8361f4279e0d63d7`
- 本文件隨後的 checkpoint commit 會包含目前所有未完成修正；請以遠端 branch
  tip 為準，不要只停在 `94f70d4`。
- 正式 SSOT 仍是 `main`。Gate 2 最終只應以 PR 合入 `main`，不要直接在
  downstream branch 間互相 merge。

公司端接手：

```powershell
git fetch origin --prune
git switch feat/gate2-governance-ledger
git pull --ff-only
git status --short --branch
```

## 2. 已提交的 Gate 2 基礎

以下內容已在 branch 歷史內，不是本次 WIP：

| Commit | 內容 |
|---|---|
| `420ba02` | governance contracts v1 |
| `b3c9bb9` | ingestion classification propagation |
| `a33e6d3` | classification reconciliation |
| `7ddeca1` | runtime hierarchy floors |
| `03901ef` | canonical clearance |
| `f455d4a` | retrieval、SourceSnapshot/Citation、G9 reindex |
| `6e99664` | reindex counter correction |
| `ec4ee06` | durable auth/session/PKI/CRL |
| `b008af7` | classified memory governance |
| `77264ed` | SourceSnapshot PostgreSQL fixture trace id |
| `2756676` | Task/TaskRun/Policy/Audit ledger 與 G19 pilot inventory |
| `078415b` | Gate 2 CI acceptance matrix |
| `94f70d4` | orchestration ordering 與 PostgreSQL ledger tests |

在目前 WIP 之前曾取得的證據包括：CSP full suite `1009 passed / 16 skipped`、
anila-core `803 passed / 10 skipped`、ingestion-worker `185 passed`；另有
SourceSnapshot PG `10 passed`、memory PG `2 passed`、auth PG `3 passed`、ledger PG
`3 passed`。這些是較早 checkpoint 的結果，**不能代表目前 WIP tip 仍全綠**。

## 3. 目前 checkpoint 內的 WIP

### 3.1 Orchestration ordering

- `/v1/chat/completions` 先建立 TaskRun，再進 retrieval、memory、prompt mutation
  或 foreground inference。
- pilot deny、classification propagation failure、retrieval/memory failure會嘗試關閉
  active TaskRun，並維持 zero-egress。
- nested retrieval/memory inference 可沿用 outer TaskRun，且不提前 terminalize。
- pilot overlay 暫時 hard-disable post-turn memory（`ENABLE_MEMORY=false`），避免在
  foreground TaskRun 結束後另行推論。

這一段尚未完成 durable usage/trace closure，且目前有兩個 focused test 失敗，見
第 5 節。

### 3.2 Signed pilot admission（部分完成）

- `anila-security` 新增 immutable `VerifiedPilotAdmission`／`PilotTarget`。
- signed profile 開始綁定 exact callsite、model name/type、endpoint URL、registry
  classification ceiling、collection ids、data ceiling 與有效期間。
- CSP startup cache 改保存完整 admission，runtime helper 每次重驗 expiry、target、
  collection 與 classification ceiling。
- nginx `/router/` 改由 posture snippet 控制；base 使用 enabled snippet，pilot overlay
  置換為 `return 403`。
- proxy sink 正在改成 Task → TaskRun → registry 鎖序，registry reader 改 `FOR SHARE`，
  並以 locked Task 現況重算 effective classification。

尚缺：

- Task create、RAG、memory 與所有 inference sink 的完整 collection/data-ceiling wiring。
- exact target 的完整 positive/negative runtime tests。
- `model_type=agent` 冒充 chat model 的 zero-egress test。
- pilot `/router/*` 實際 nginx container smoke。
- 簽章 deployment digest／image identity 的最終契約（目前只到 registry target）。
- profile withdrawal/reload；現況只有每次 expiry 重驗。

### 3.3 Classification monotonic lock（部分完成、未驗收）

額度中止前，Copernicus agent 在 shared worktree 留下尚未提交的修正：

- `apply_classification()` mutation path 改用
  `populate_existing + SELECT ... FOR UPDATE`。
- declassification request/approval 鎖列後重讀；資源在申請後升級時，舊降級申請
  fail-closed。
- 新增 SQLite stale identity-map tests 與
  `test_gate2_classification_latch_pg.py` true-PG barrier tests。

尚缺：

- true-PG 測試尚未用正確的公司／disposable DB credential 跑通。
- `retrieve_and_seal()` 仍須在最後 seal transaction 固定 Task → SourceSnapshot 鎖序，
  refresh Task 後做 monotonic max；否則 concurrent raise 仍可能被 stale Task 覆寫。
- active TaskRun classification 必須在每次 Task raise 與 finalize 時同步到最高值。

## 4. 獨立審查的 Gate blocker

Parfit agent 的結論是 **Request changes**。修正順序建議如下：

1. **P0 signed pilot bypass**：完成 exact target/type/endpoint/deployment identity，pilot
   `/router/` hard deny，拒絕 Agent-as-ModelRegistry type confusion。
2. **P0 classification one-way latch race**：完成 canonical policy、retrieval seal、sink
   locked-current ceiling 三個 race proof。
3. **P1 signed scope 被丟棄**：Task create、RAG、memory、sink 全部執行 collection ids、
   data ceiling、expiry/withdrawal。
4. **P1 Task/TaskRun deadlock**：stale reconciler 目前是 Run → Task；改成先取 candidate
   ids，再逐筆 Task → Run `FOR UPDATE SKIP LOCKED` 並重驗 stale/status。
5. **P1 retrieval audit atomicity**：SourceSnapshot/Citation/Task classification 與
   `collection.read allow` PolicyDecision/Audit 必須同 transaction，或使用 durable outbox。
6. **P1 durable usage**：目前 TokenUsage 仍走 process-memory queue；TaskRun 可先 completed
   而 usage 永久遺失。Agent 路徑另錯用 `agent.id` 當 `model_registry.id` FK。需同交易寫入
   或 durable outbox、retry、poison isolation，並可靠連結 `TaskRun.usage_record_id`。
7. **P1 clearance revoke race**：
   `ClearanceGrantCompartment` membership query 補 `FOR SHARE`，並加 reader/revoker
   PostgreSQL barrier test。
8. **P1 Full Trace**：nested retrieval/memory 仍缺 child spans，retrieval usage trace 可為
   NULL，non-stream Agent 手寫 httpx 路徑繞過 proxy spans。usage/span/finalize 應透過同一
   durable closure/outbox。
9. **P1 registry concurrency**：WIP 已改 reader 為 `FOR SHARE`，仍要加 concurrent
   same-model inference 與 registry admin update 的 PG test。
10. **P1 lifecycle**：同一 Task 只能一個 active TaskRun；aborted transaction 後的
    terminalization 必須 fresh-session retry；finalize 必須鎖存最高 classification。

P2 後續：Task-linked `X-ANILA-Trace-Id` 必須等於 canonical Task trace；非 pilot 的
standalone search 不應用 collection level 代替 query authority；post-turn memory 應有獨立
TaskRun/outbox；SourceSnapshot orphan payload file 需 reconciliation。

## 5. 2026-07-13 checkpoint 驗證

已通過：

```text
anila-security + pilot policy focused tests: 46 passed
Ruff（本輪 security/policy/proxy/classification files）: PASS
git diff --check: PASS
```

目前已知失敗：

```text
CSP focused: 108 passed, 2 failed

1. test_task_run_precedes_retrieval_memory_and_foreground_send
   測試 double 建出的 MemoryReadResult 沒有 inherited_classification，
   但 proxy.py post-turn scheduling 直接讀該欄位。

2. test_governance_stage_failure_is_zero_egress_and_closes_open_run
   latch case 預期 memory_classification_latch，實際先被 memory_failed 捕捉。
```

另外：

- `test_gate2_classification_latch_pg.py` 兩案尚未執行到 assertion；本機 disposable PG
  的舊 credential 無法登入。請用公司端新建、已 migration 的 disposable DB 重跑。
- compose config 尚未驗證：此 worktree 沒有 `.env`，`.env.example` 也沒有正式
  `ANILA_IMAGE_*@sha256` 值。不要為了 config 把假 digest 或 secret commit 進 repo。
- Docker pilot、air-gap `--no-build --pull never`、non-root/mount/ACL、HTTP auth/RAG/
  Full Trace readback 都尚未對目前 WIP tip 重跑。

## 6. 公司端第一輪操作

先確保 Python import 指向目前 checkout，而不是其他 Codex worktree：

```powershell
python -m pip install -e ./packages/anila-contracts -e ./packages/anila-security -e './packages/anila-core[dev,rag]' -r services/csp/requirements-dev.txt
$env:PYTHONPATH='packages/anila-security/src;packages/anila-contracts/src;services/csp'
python -m pytest packages/anila-security/tests/test_pilot_profile.py infra/policy/tests/test_gate2_pilot_inference_profile.py -q
python -m pytest services/csp/tests/test_gate2_pilot_runtime_admission.py services/csp/tests/test_gate2_orchestration_ordering.py services/csp/tests/test_startup_security.py services/csp/tests/test_classification_upgrade.py -q
```

接著依第 4 節順序處理 blocker。每關都要補 mutation-sensitive true-PG test；不要只讓
SQLite fixture 綠燈。

建議用全新 disposable PostgreSQL，跑 Alembic 到 single head 後設定：

```powershell
$env:ANILA_GATE2_CLASSIFICATION_PG_URL='<由公司 secret store 提供的 disposable DB URL>'
python -m pytest services/csp/tests/test_gate2_classification_latch_pg.py -q
```

完整 PG matrix 仍包含 classification reconciliation、runtime hierarchy、clearance/RLS、
auth、memory、SourceSnapshot/Citation、ledger、reconciler/finalizer、usage/outbox 與
clearance revoke races。

## 7. 最終 closeout 條件

完成 blocker 後才可進入以下流程：

1. CSP/core/worker/full frontend/Studio/FLUX 全套測試。
2. 全部 Gate 2 true-PG matrix 與 mutation-sensitivity。
3. 正式 locked images 建置；另起乾淨環境做 air-gap
   `docker compose up -d --no-build --pull never`。
4. pilot `/router` hard deny、signed target positive/negative、non-root、mount/ACL、正式
   HTTP auth、RAG、SourceSnapshot/Citation、usage/Policy/Audit/TraceSpan/TaskRun 回讀。
5. 三位獨立具名 AI reviewer 重新依最終 head 出具 `Approve` 或 `Request changes`。
6. GitHub required CI 全綠、所有 review threads 回覆並 resolve。
7. 更新本文件為 final handoff（移除 WIP 標記、加入最終 commit/PR/merge commit 與
   redacted evidence hash），才可 merge 與宣告 Gate 2 完成。

## 8. 安全提醒

- 不得提交 `.env`、signed pilot approval、trust-store private material、JWT/TLS keys、
  DB URLs、token、完整內網 hostname/IP 或原始敏感 runtime evidence。
- repo 只留 schema、disabled template、redacted hash/結果與可重現指令。
- runtime DB 使用 `csp_app`；migration/fixture setup 才能使用 admin role。
- 目前 checkpoint 是為了跨環境續作而 push，**不是 merge approval**。
