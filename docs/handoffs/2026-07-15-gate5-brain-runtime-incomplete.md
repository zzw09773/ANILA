# Gate 5 Brain Runtime handoff（INCOMPLETE）

日期：2026-07-15

## 目前定位

- Branch：`codex/gate5-brain-runtime`
- Base：`prod-intranet-card`
- Draft PR：<https://github.com/zzw09773/ANILA/pull/31>
- Implementation checkpoint（handoff 前）：`c106993813aca6925cf5f90a94ae5dd37b2276ba`（`c106993`）
- Handoff artifact commit / current branch head：請用 `git log -1 --format=%H -- docs/handoffs/2026-07-15-gate5-brain-runtime-incomplete.md` 與 `git rev-parse HEAD` 讀取，避免 self-reference SHA 失真。
- Gate 5：**INCOMPLETE**。
- **不得 merge，也不得進入 Gate 6。** Claude Fable review 尚未執行。
- Current CI：`c106993` 新 runs `29370210595`、`10617`、`10724` 已回報多個 failure（Deployment contracts、Gate 5 static/routing、Contract smoke、`anila-core`、`anila-agent`、governance、image-lock）；CSP 仍 in progress。`ingestion-worker` 與 Python security 已 pass，表示 resolver 至少解除部分安裝阻塞；仍需重新查看新 logs，required CI 尚未綠。

## R1–R7 狀態

| R | 狀態 | 已完成範圍 |
|---|---|---|
| R1 | implemented / locally verified | Contracts v2 |
| R2 | implemented / locally verified | Readiness |
| R3 | implemented / locally verified | Structured `RouterRuntime` / metrics |
| R4 | PARTIAL | `AgentClient` + `StreamBridge` + PostgreSQL event store；restart 後正式 E2E 尚未證明 |
| R5 | PARTIAL | Official Agent Silver foundation；restart 後正式 E2E 尚未證明 |
| R6 | implemented / locally verified | Frozen evaluation |
| R7 | PARTIAL | Signed model governance、central CSP receipts、formal egress topology；packet firewall、operator-signed 材料、完整 38-callsite wiring 尚未完成 |

R1/R2/R3/R6 的 implemented / locally verified 仍不代表 required CI 已綠；R1–R7 的完成度也不等於 Gate 5 close。下列 blocker 仍必須關閉並重新驗證。

## Local evidence

- `packages/anila-core`：875 passed、11 skipped。
- `services/csp`：1432 passed、38 skipped。
- True PostgreSQL：7 passed。
- Gate 5 static：47 passed。
- Routing：14 passed；top-1 `60/60`、false `0/90`、bypass `0/20`。
- Deployment/capability：14 passed。
- `c106993` 後 resolver dry-run：PASS。

## Gate 5 blockers（不可省略）

1. 尚無 packet firewall。
2. 尚無真正的 operator-signed production profile、trust/legal materials；因此 FLUX 維持 disabled。
3. 尚無 formal Docker live E2E / network-negative smoke。
4. 仍有 38 個 callsites 尚未全部接上 runtime admission/usage wiring。
5. 尚未證明 restart 後 `Router → CSP signed sink` 的 pause/approve/resume 流程。
6. GitHub required CI 尚未綠。

## 舊 `a9ce759` runs 的失敗根因

以下只記錄 `a9ce759` 舊 runs 的已知根因；不可直接套用到 `c106993` 新 runs。新 runs 已回報不同或額外 failure，仍需重新查看新 logs，不能只沿用 `a9ce759` 根因；`c106993` 目前只能確認修正 contracts v2 resolver：

- contracts v2 resolver 失敗（已由 `c106993` 修正）。
- Static PR merge SHA expected 錯誤。
- 4 個 skip baseline 未登記。
- `flux2-dev-agent` stale image mapping。
- Backup profile 缺少 4 個 GATE5 material binds；extra 只有 `../../share/uploads/flux` 一個。
- Airgap count 預期 13、實際 12。
- Deploy test 仍期待 flux token。

## True PostgreSQL 測試環境

| 欄位 | 值 |
|---|---|
| Container | `anila-gate5-pgtest` |
| Host/port | `127.0.0.1:5544` |
| Database | `csp` |
| User | `csp` |
| Password | `gate5test`（僅本機測試） |
| Alembic | `r1_0027` |

測試完成後清理：

```powershell
docker rm -f anila-gate5-pgtest
```

## Exact resume commands

```powershell
Set-Location C:\Users\USER\.codex\worktrees\ANILA-gate5
git status --short --branch
git rev-parse HEAD
git log --oneline --no-merges origin/prod-intranet-card..HEAD
```

目前最新 commit：

```text
c106993 [security-all] align runtime packages with contracts v2
```

Gate 5 commit list（起點 `e8d6989` 至 HEAD，共 19 commits）：

```text
c106993 [security-all] align runtime packages with contracts v2
a9ce759 [security-all] govern CSP model egress receipts
ccb1530 [security-all] complete formal Agent dispatch boundary
c38a4e6 [security-all] enforce formal model egress boundary
e377738 [security-all] fix: refresh model governance readiness
dd6282f [security-all] feat: bootstrap signed model governance
840046d [security-all] feat: promote official Agent to Silver
2039f03 [security-all] test: enforce Gate 5 routing metrics
07c11fb [security-all] feat: wire formal Router runtime seams
7aa1227 [security-all] feat: verify model governance authority
edc1ca2 [security-all] test: prove durable event races on PostgreSQL
2654c53 [security-all] feat: issue signed ExecutionGrant envelopes
dbc6c0b [security-all] feat: persist durable session events
2da0216 [security-all] fix: type Agent readiness DB session
75ce7d0 [security-all] feat: enforce authoritative Agent readiness
9ce7ee9 [security-all] feat: add durable StreamBridge contracts
19b0083 [security-all] test: freeze Gate 5 routing evaluation set
fa517f9 [security-all] feat: add structured RouterRuntime core
e8d6989 [security-all] feat: add Gate 5 governance contracts v2
```

`origin/prod-intranet-card` 尚未收進 prior Gate history，因此完整 branch delta（含 prior Gate/merge）請另查：

```powershell
git log --format='%h %s' origin/prod-intranet-card..HEAD
```

不要把上述 prior Gate commits 冒充 Gate 5。

## Resume order

1. 先修正並重新查證 GitHub required CI；`c106993` push 後目前正在重跑，狀態須重新查。
2. 補齊 operator-signed production profile/trust/legal materials 與 packet firewall；材料核准前維持 FLUX disabled。
3. 補 formal Docker live E2E、network-negative smoke、完整 38 個 runtime admission/usage wiring，並以可重啟實例證明 `Router → CSP signed sink` pause/approve/resume。
4. 最後執行 Claude Fable review；只有 blocker 關閉且證據重跑通過後，才可標記 Gate 5 complete 並評估進 Gate 6。
