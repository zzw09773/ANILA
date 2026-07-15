# Gate 5 Brain Runtime handoff（INCOMPLETE）

日期：2026-07-15

## 目前定位與 checkpoint 真實狀態

- Branch：`codex/gate5-brain-runtime`
- Base：`prod-intranet-card`
- Draft PR：[#31](https://github.com/zzw09773/ANILA/pull/31)
- 功能 code checkpoint：`1d9ce24d4be965855acf9c98ce0056a28435e322`（`1d9ce24 [security-all] harden Gate 5 resume and model egress`）。本輪已 push。
- Handoff 前 implementation/CI-fix checkpoint：`847f217ec44ff13df7d34e72219d8a42da7e7302`（`847f217 fix(security): [security-all] preserve service-client proxy isolation`）。
- Handoff artifact／current branch HEAD：本 handoff artifact commit/push 後 HEAD 會前進；請用下列命令 read-back，避免預填本檔自身的未知 SHA：

```powershell
git log -1 --format=%H -- docs/handoffs/2026-07-15-gate5-brain-runtime-incomplete.md
git rev-parse HEAD
```
- CI 修正鏈：`62041599bfd5544e34c1279b330faefb096d76d6`（`6204159 fix(ci): [security-all] make Gate 5 generator tests unittest-compatible`）→ `eb02a960afa76b682efdac4343c143b9e9105cef`（`eb02a96 style(ci): [security-all] format Gate 5 generator contract`）→ `847f217`。
- 前一個 CI dependency checkpoint：`86d33167bc3aa6b4682748c7e271df9f9bed1be7`（`86d3316 [security-all] install Gate 5 CI runtime dependencies`）。
- `eb02a96` 的 CI read-back：CSP full 為 `2 failed / 1441 passed / 38 skipped`，Python security 為 `1 failed`。兩者共通的 service-client regression 已由 `847f217` 修正；CSP 另一個 runtime failure 已確認是 registered-agent category test drift，並已精準修正測試。
- `847f217` implementation checkpoint 的 runs 全部 completed success：security run `29412637564`、Gate 5 run `29412637562`，以及 main run `29412637572`。Main run 的 CSP full suite PASS（9m26s）、PostgreSQL RLS PASS（2m28s），其餘列出的 FLUX、PPTX、Studio、anila-agent、anila-core、ingestion、Contract smoke、image-lock、frontends 與 governance 亦均 PASS；該 code checkpoint 的 required checks 全綠。
- 這是 `847f217` code checkpoint 的 CI read-back，不代表 Gate 5 已 close；handoff artifact commit/push 後仍須重新 read-back 以防狀態漂移。
- Gate 5：**INCOMPLETE**。
- Router 跨重啟 durable resume authority context 與 CSP→Router caller provenance 尚未完成；因此不可 production-ready、不可進行 Claude Fable review、不可 merge，也不可進入 Gate 6。

## 本輪已落地的 runtime seam

- 正式 Router → CSP → Agent binary `approve_all` in-process resume seam。
- CSP event rebind；`BLOCKED` 維持 nonterminal，不再 synthetic `COMPLETED`。
- Agent `FileTaskStore` restart/idempotency 行為。
- Router resume cache：bounded 256 entries、15 分鐘 TTL、不保存 bearer、terminal 取出即移除；cache miss 回 `409`。

上述是 in-process／現行程序生命週期內的 seam，尚不能當成跨 Router 重啟的 durable authority proof。

## R1–R7 狀態

| R | 狀態 | 已完成範圍 |
|---|---|---|
| R1 | implemented / locally verified | Contracts v2 |
| R2 | implemented / locally verified | Readiness、Agent readiness 與 execution grant |
| R3 | implemented / locally verified | Structured `RouterRuntime`、dispatch 與 metrics |
| R4 | PARTIAL | `AgentClient`、`StreamBridge`、durable PostgreSQL event store；跨 Router 重啟 authority context 尚未證明 |
| R5 | PARTIAL | Official Agent Silver foundation、`FileTaskStore` restart/idempotency；正式跨重啟 E2E 尚未證明 |
| R6 | implemented / locally verified | Frozen deterministic evaluation |
| R7 | PARTIAL | 40-callsite model inventory、proxy 分流、CSP receipts、internal model network 與 disabled FLUX governance；production provenance 與完整重啟證據仍待補 |

R1/R2/R3/R6 的 locally verified 與 `847f217` code checkpoint required checks 全綠，仍不等於 Gate 5 close。

### R7 目前實測範圍

- Model inventory 共 40 個 callsites；generic proxy 與 agent-scoped proxy callsites 已拆開。
- 只有具 verified `CallerIdentity` 的 agent csk 才能走 agent-scoped proxy；單獨 forged header 在該 admission path 無效。
- DB receipts 使用實際 attribution；synthetic default 僅 memory-only。
- External material verifier 明確使用 `--repo-root`。
- FLUX profile/readback/legal 維持 disabled。
- Model network 使用 internal bridge；完整 direct-IP Docker negative smoke 的 initial／restart／recreate 三階段均 PASS。

這不等於 CSP→Router 的 caller provenance 已完成：`X-ANILA-Caller-User-Id` 目前雖會比對 cached identity／owner，但 header 尚未有 authenticated/signed provenance，仍可被 spoof。

## Local validation evidence

- Policy/deployment：`36 passed`。
- Agent：`19 passed`。
- Core：`24 passed`。
- CSP：`37 passed`；後補 CSP R7：`15 passed`。
- 本機 security four-file：`67 passed`。
- 本機 runtime+R7：`20 passed`。
- Ruff／`git diff --check`：PASS；另有 `compileall`、3 個 scripts 的 `bash -n` 通過。
- Disabled governance verifier：PASS；inventory 40，hash `e49b...`。
- Docker smoke：`49.4s PASS`。
- 本機 model network 已安全重建為 `Internal=true`、`Driver=bridge`，目前無 attached containers。
- GitHub implementation checkpoint read-back：CSP full suite 與 PostgreSQL RLS 均 PASS（不在此處虛構測試數）。

上述測試套件有重疊；各套件數字不可相加成一個唯一總數。

## Gate 5 blockers（不可省略）

1. Router 跨重啟的 durable resume authority context 尚未完成；目前 cache／in-process seam 不能取代 durable record 與 renewed grant。
2. CSP→Router `X-ANILA-Caller-User-Id` 的 authenticated/signed provenance 尚未完成；目前 cached identity／owner 比對不足以防 header spoof。
3. 真 PostgreSQL／container restart E2E 尚未補齊，尚未以可重啟實例證明 pause／approve／resume 全鏈路。

## Exact resume commands

```powershell
cd C:\Users\USER\.codex\worktrees\ANILA-gate5
git status --short --branch
git log -1 --format=%H -- docs/handoffs/2026-07-15-gate5-brain-runtime-incomplete.md
git rev-parse HEAD
git log -5 --oneline --decorate
git log --oneline --no-merges origin/prod-intranet-card..HEAD
gh pr checks 31
```

Focused runtime／governance tests：

```powershell
python -m pytest `
  packages/anila-core/tests/test_router_resume_proxy.py `
  packages/anila-core/tests/test_e2e_ask_user_resume.py `
  packages/anila-core/tests/test_router_session.py `
  packages/anila-agent/tests/test_streaming.py `
  services/csp/tests/test_gate5_r4_durable_session_event_store.py `
  services/csp/tests/test_gate5_r4_stream_bridge.py `
  services/csp/tests/test_gate5_model_governance_receipts.py `
  services/csp/tests/test_gate5_proxy_model_governance.py `
  infra/policy/tests/test_gate5_model_governance.py `
  infra/policy/tests/test_gate5_deployment_egress.py
```

Docker negative smoke 與 network read-back：

```powershell
bash infra/deployment/scripts/gate5-network-negative-smoke.sh
docker network inspect anila-models-net --format 'Internal={{.Internal}} Driver={{.Driver}} Containers={{len .Containers}}'
```

## 下一步（只做 Gate 5 blocker closure）

1. 設計並落地 CSP durable resume record／renewed grant，補 Router restart E2E。
2. 建立 CSP→Router authenticated/signed header provenance，補 forged/spoof header negatives。
3. 補真 PostgreSQL／container restart E2E，證明 durable pause／approve／resume。
4. Resume 時先重新 read-back `gh pr checks 31` 與 current HEAD，防止 CI/status drift；若 head 或 required checks 改變，再補測並重跑 CI。
5. 上述 blocker 與證據全部閉合後，最後才用 `claude -p`、`--model claude-fable-5`、`--effort max` 做 review；在此之前不進 Gate 6。

## Commit list / head provenance

目前 head 與最近 checkpoint：

```text
847f217 fix(security): [security-all] preserve service-client proxy isolation
eb02a96 style(ci): [security-all] format Gate 5 generator contract
6204159 fix(ci): [security-all] make Gate 5 generator tests unittest-compatible
1d9ce24 [security-all] harden Gate 5 resume and model egress
86d3316 [security-all] install Gate 5 CI runtime dependencies
e78d658 docs: update Gate 5 CI checkpoint
cd82f17 [security-all] align Gate 5 CI governance surfaces
2a3d9cc docs: clarify Gate 5 checkpoint provenance
05ba86f docs: hand off incomplete Gate 5 checkpoint
```

Gate 5 implementation lineage（起點 `e8d6989`；不把 prior Gate commits 冒充 Gate 5）：

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

`origin/prod-intranet-card` 尚未收進 prior Gate history；若需完整 branch delta，另查：

```powershell
git log --format='%h %s' origin/prod-intranet-card..HEAD
```

本 handoff artifact commit/push 會使 branch HEAD 前進；不要預填本檔自身的未知 SHA。完成後請用上列 `git log -1 --format=%H -- docs/handoffs/2026-07-15-gate5-brain-runtime-incomplete.md` 與 `git rev-parse HEAD` read-back。
