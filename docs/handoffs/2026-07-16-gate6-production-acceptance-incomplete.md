# Gate 6 Production Acceptance durable handoff（INCOMPLETE / NO-GO）

更新時間：2026-07-16 05:00:12（UTC+8）

## 先看結論

- **Gate 6 engineering checkpoint 的變更已提交並推送；Production Acceptance 結論仍為 No-Go。**
- Gate 6 branch 目前為 `bdf1177495ca6364e2a1bb22915482a76b2d8e97`；draft PR
  [#32](https://github.com/zzw09773/ANILA/pull/32) 為 `OPEN / MERGEABLE / Draft`。
  PR 不得 merge、不得改 ready，也不得據此宣告 Go。
- P0 schema／verifier、disabled template、P9 inventory exporter、backup contract、CI
  contract 與目前的測試綠燈，只證明 repository-side engineering contract 的部分邊界；
  **不能因此宣告 P0、P1、P9 pass，更不能宣告 P0–P9 全數通過或 production Go。**
- Gate 6 的 P3 七日觀測、P6 獨立具名人類覆核、P7 法務授權、P8 實體卡矩陣，以及
  P0–P9 的五方人員簽核，都是不可由 schema／CI／AI 壓縮的外部 blocker。
- Gate 5 已 merge；這個 checkpoint 只是從 Gate 5 進入 Gate 6 的工程收尾，不是
  Production Acceptance 核准。

## Provenance（可恢復的 repo 狀態）

| 欄位 | 值 |
|---|---|
| Worktree | `C:\Users\USER\.codex\worktrees\ANILA-gate6` |
| Branch | `codex/gate6-production-acceptance` |
| Base | `main`；Gate 5 merge `bca5af0cc091dac231903d5b9cfa3dc66d2fa95d` |
| Gate 6 commits | `6a126870d760c7d34b2632fe642229c3a878f00c`、`d545630f38336f1e99190ef360711aa0df2ea71d`、`bdf1177495ca6364e2a1bb22915482a76b2d8e97` |
| Current committed HEAD | `bdf1177495ca6364e2a1bb22915482a76b2d8e97`（Gate 6 current head） |
| Gate 5 PR | [#31](https://github.com/zzw09773/ANILA/pull/31)；merge commit `bca5af0cc091dac231903d5b9cfa3dc66d2fa95d` |
| Gate 6 PR URL / number | [#32](https://github.com/zzw09773/ANILA/pull/32) |
| Gate 6 PR state / draft / mergeability | `OPEN / Draft / MERGEABLE`；不得 merge 或改 ready |
| Gate 6 pushed head SHA | `bdf1177495ca6364e2a1bb22915482a76b2d8e97` |
| Remote | `origin=https://github.com/zzw09773/ANILA.git` |
| Working tree | 05:00 snapshot 只有 handoff untracked；本文件後續將 docs-only commit/push，發布後以 `git rev-parse HEAD` 讀回包含 handoff 的 PR head；last reviewed code head 仍為 `bdf1177495ca6364e2a1bb22915482a76b2d8e97` |

目前的 `HEAD` 是 Gate 6 current head，不是 Gate 5 merge；`bca5af0…` 僅是 base。後續
CI／review 仍必須指向 `bdf1177…`，不可把較早的 `6a12687…` 或 `d545630…` 誤當最終 head。

## 本 checkpoint 的具體變更與用途

以下是目前 Gate 6 engineering checkpoint 的檔案範圍；本 handoff 不表示其中任何一項
已滿足完整 Production Acceptance：

- `.github/workflows/gate6-production-acceptance.yml`：Gate 6 repository posture、
  policy/backup contracts、static workflow/lint/diff/YAML checks；job 名稱與說明刻意
  表達為 engineering contract，不能產生 production approval。
- `infra/ci/check_gate6_repository_posture.py`：fail-closed repository posture checker；
  驗證 P0 只有 disabled template、P9 只能走 disabled inspection mode，並拒絕 checked-in
  trust store、signature、private key 或 production evidence。
- `infra/policy/gate6/README.md`：P0 profile schema、欄位、五方 signer role、canonical
  hash／signature 與 fail-closed 規則；明確說明 repository 不產生 key 或 production approval。
- `infra/policy/gate6/README-p9.md`：P9 exporter 的輸入／輸出契約與尚未完成的 runtime
  wiring、network capture、egress deny、usage reconciliation 邊界。
- `infra/policy/gate6/production-acceptance-profile.disabled-template.json`：明確
  `enabled=false`、`template_only=true`、`approval_status=disabled_template`、空
  signatures／空 content hash 的 P0 authoring template；不是 production profile。
- `infra/policy/gate6/generate_p9_enabled_callsite_inventory.py`：以 Gate 5 model
  governance authority 及 P0（enabled 時）交叉驗證，產出 deterministic machine-readable
  P9 callsite evidence；`gate6_pass` 固定為 `false`。
- `packages/anila-security/src/anila_security/production_acceptance_profile.py` 與
  `packages/anila-security/src/anila_security/__init__.py`：P0 exact-set schema、效期／
  observation window、profile hash、五方 Ed25519 signature 與 Gate 5 inventory binding
  的 validator／verifier 及公開匯出。
- `packages/anila-security/tests/test_production_acceptance_profile.py`：驗證 profile
  hash、完整五方簽章、P0↔Gate 5 inventory cross-check、tamper／unsigned／disabled
  template fail-closed 與 CLI 行為。
- `infra/policy/tests/test_gate6_p9_enabled_callsite_inventory.py`：P9 exporter 的
  deterministic evidence、P0 required arguments、disabled mode 與 fail-closed contract。
- `infra/policy/tests/test_gate6_repository_posture.py`：P0 disabled template、P9 disabled
  mode 與 repository production-evidence exclusion contract。
- `infra/deployment/backup/production_backup.py`：backup capture、age encryption、
  disposable restore smoke 的 subprocess launch／communication error wrapping 與清理
  hardening。
- `infra/deployment/tests/test_production_backup_automation.py`：backup／pg_dump／docker
  readiness／container cleanup 啟動失敗與 disposable restore smoke 的測試覆蓋。

## P0–P9 狀態矩陣

| 項目 | 目前狀態 | 已有／尚有的精確缺口 |
|---|---|---|
| **P0** | **PARTIAL；Production Acceptance 不成立** | schema、verifier、disabled template 與五方 signer role／簽章規則已有 code/test；但尚缺由 system owner、data owner、PKI owner、資安、維運完成的 production signed acceptance profile（含實際 topology、feature、資料上限、RTO/RPO、SLO、load、觀測窗、workflow matrix、callsite inventory、revocation／PKI policy、finding rule、impact matrix 等）。不能把 synthetic test signatures 當成 production 五方簽核。 |
| **P1** | **PARTIAL；No-Go** | 已有 backup launch hardening 與 disposable Docker restore smoke；尚缺 production-equivalent restore、P0 RTO/RPO 內計時、DB／blob／artifact／active vector generation checksum／referential integrity，以及 RLS、compartment、revocation 後讀取 negative tests 的完整 drill。 |
| **P2** | **NOT DONE** | Redis／worker／CSP、磁碟滿、網路中斷等 fault drills、lease/retry 終態、RPO/RTO reconciliation 與 fail-closed 證據尚缺。 |
| **P3** | **BLOCKED / NOT DONE** | signed load profile 下連續 7 日的 time-locked observation 尚未完成；沒有觀測窗與未處置 Sev-1／Sev-2 的證據。 |
| **P4** | **NOT DONE** | signed release envelope、單一 code line 對應的 image/model digest、SBOM、CA bundle hash、deployment topology／feature／data ceiling、bundle signature 與 clean-host air-gap deploy 尚缺。 |
| **P5** | **NOT DONE** | P0 先凍結的 signed workflow matrix、各登入法／分類／compartment 的正負 fixture、固定 N 的 production-like 全鏈（upload → chunk → retrieval → citation → artifact → trace）抽驗與逐筆證據尚缺。 |
| **P6** | **BLOCKED** | 不同於修補者的獨立、具名人類 reviewer 與 signed review report 尚缺；AI／Claude 不能取代 P6 reviewer。 |
| **P7** | **BLOCKED** | 每個 enabled model、dataset、第三方元件適用於軍方環境的法務／採購書面授權裁決尚缺；未核准能力必須從 signed profile、bundle、registry、routing capability 排除。 |
| **P8** | **BLOCKED** | production-equivalent 實體卡、reader、支援 OS/browser、HiPKI `localhost:16888`、卡型、X.509/CRL、revocation／refresh reuse／`jti`／break-glass／race／時間偏差矩陣尚缺；離線或 stale revocation 的 production fail-closed 證據尚缺。 |
| **P9** | **PARTIAL；No-Go** | P0↔Gate 5 machine-readable enabled callsite inventory exporter 已有；但 runtime wiring／admission binding、host/container/network deny 與 packet capture、裸 endpoint negative tests、usage／audit reconciliation、以及五方 acceptance sign-off 尚未完成。 |

**結論：P0–P9 不是全綠；任一項缺 owner、evidence、門檻或事前凍結的 profile，結論都
是 No-Go。**

## 已確認的驗證 evidence（僅工程 contract）

以下數字採 root 最新已確認結果；它們不是 production acceptance pass：

| 範圍 | 結果 |
|---|---|
| `anila-security` | **65 passed**（含新增 loader tests） |
| Gate 5 + P9 focused policy contracts | **30 passed（Python 3.11）**；Docker Python 3.12 **30 passed**；Docker 已清理 |
| Gate 6 repository posture | **8 passed** |
| Production backup automation | **27 passed / 1 skipped** |
| Combined sequential Gate 6／Gate 5 suite（Fable fixes 後） | **131 passed / 1 skipped** |
| Disposable Docker restore smoke（另跑） | **1 passed / 27 deselected**，9.93s；container／volume 無殘留 |
| Ruff／compileall／`git diff --check` | **PASS** |
| Gate 6 workflow posture／static checks | **PASS** |
| PR #32 code-head CI snapshot | **23/23 checks 全綠**（2026-07-16 04:36 UTC+8）；`gh pr checks 32` exit 0 |

先前並行 source-scanner 曾因 shared-worktree temporary fixture race 失敗；該競態不是產品
failure，序列重跑的 current Gate 5／P9 suite 為 30 passed，且工作樹／Docker 無殘留。不得
把這些工程 contract 結果擴寫成 P9 runtime network 或 usage 已驗收。

### CI run provenance（均指向 Gate 6 current head）

- Run `29445861023`：因缺少 local `anila_core` install 失敗；已修正。
- Run `29447399506`：Python 3.11 pass；Python 3.12 因 PEP 701 f-string scanner 失敗；
  scanner 已修正。
- Run `29448123678`：PR #32 code head `bdf1177495ca6364e2a1bb22915482a76b2d8e97` 的
  23 個 checks 於 `2026-07-16 04:36:01`（UTC+8）read-back 全部 pass；`gh pr checks 32`
  exit 0。這是 code-head CI 全綠證據，不是 production approval。
- CSP full suite job `87463604849`：**success**，耗時 `8m59s`。
- PostgreSQL RLS job `87465627161`：**success**，完成於 `2026-07-16 04:36:01`（UTC+8）。
- Gate 6 Python 3.11 job `87463600938` 已於 `2026-07-15T20:26:06Z`
  （UTC+8 `2026-07-16 04:26:06`）成功完成。

## Argus provenance

- Workflow：**Opus 4.8 xhigh read-only Workflow**；workflow child：`claude-sonnet-5` 子任務。
- Session ID：`ec0aae0d-7d6d-424e-905f-e5422b072318`。
- Workflow ID：`wf_65cc3531-b18`。
- Final synthesis：**完成，10/10 tasks、0 error；頂層 verdict = No-Go**。
- 這份 Argus synthesis 是 read-only engineering／security evidence，**不得把它當 P6
  獨立具名人類 reviewer，也不得代替五方人員簽核**。

### Argus 補充的尚未關閉 blocker

- **P9**：ingestion relation-LLM／Judge／evaluator、Studio／FLUX、Agent embedding／
  memory 等 inference path 尚未全面證明經 CSP gateway／receipt；runtime wiring、network
  capture／deny 與 usage reconciliation 仍未完成。
- **P2**：disk-full fault drill 尚未完成；部分 image persist 路徑可能出現 log 後靜默
  degradation，仍需 fault script、唯一終態與 fail-closed 證據。
- **P1**：尚無 P0 RTO 計時，也尚未完成完整 RLS／compartment／revocation negative
  drill；disposable restore smoke 不等於 production-equivalent restore。
- **P4**：尚無 SBOM 與 production 私鑰簽章／clean-host release envelope 證據。
- **P5**：核心跨段 compartment matrix 與 upload → chunk → retrieval → citation →
  artifact → trace 的全鏈 harness／逐筆證據尚缺。

## Fable5／security review provenance（不取代 P6）

- Initial full review session：`f54f286d-511d-4ec5-aa8d-a560b9435de2`，review head
  `6a126870d760c7d34b2632fe642229c3a878f00c`，verdict **Request changes**；finding 為
  fixed-time expiry，已修正。
- Follow-up session：`96d866b7-5f87-4e4d-92e4-b1eb2fab7b3d`，review head
  `d545630f38336f1e99190ef360711aa0df2ea71d`，verdict **Approve**，附 LOW／INFO notes。
- 同一 follow-up session 對 `bdf1177495ca6364e2a1bb22915482a76b2d8e97` 的 final
  code-head incremental review 已為 **Verdict: Approve**：確認 PEP 701 scanner fix correct，
  無 blocker。新增的 INFO 為 tokenize error 目前回傳 `[]` 的窄 fail-open，以及 future
  Python 3.14 t-string 支援提醒；兩者均不影響本 PR。
- Codex Security diff scan：**no reportable findings**；exact report path：
  `C:\Users\USER\AppData\Local\Temp\codex-security-scans\ANILA\bca5af0_20260715T194201Z\report.md`。
- Fable5、Claude、Argus 與 Codex scan 都是工程／安全證據，不是 P6 獨立具名人類 reviewer，
  也不取代五方人員簽核、法務裁決或實體卡驗證。

## 05:00（UTC+8）decision section：正式 read-back

05:00:12（UTC+8）read-back 確認：Git `HEAD` 與 PR head 均為
`bdf1177495ca6364e2a1bb22915482a76b2d8e97`，branch 為 `codex/gate6-production-acceptance`，
PR #32 為 `OPEN / Draft / MERGEABLE`；23/23 checks 為 `SUCCESS`、`notGreen=[]`，且
`gh pr checks 32` exit 0。工作樹只剩本 handoff untracked。

**正式判定：無法在 06:00 前完成 Gate 6 Production Acceptance，維持 No-Go。** 不可壓縮的
blocker 至少包括：P3 連續 7 日 time-lock、P6 獨立具名人類 reviewer、P7 法務／採購書面
裁決、P8 實體卡／reader／HiPKI 矩陣、P0 五方簽章；另 P1／P2／P4／P5／P9 的 production
evidence 仍缺。這個判定不是把工程 CI 綠燈寫成 P0–P9 acceptance，也不是 Production Go。

### 05:00 process／session／stage snapshot

- 05:00 時 workflow／review：**none running**；Fable sessions 與 Argus 均 completed。
- Claude CLI processes：**none**；Luna agents：**completed / no running**；GitHub workflows：
  **none**；Docker `python:3.12-slim` 與 `anila-backup-test-source` containers：**none**。
- stopped sessions／stage：
  - Argus session `ec0aae0d-7d6d-424e-905f-e5422b072318`、workflow
    `wf_65cc3531-b18`：completed No-Go synthesis。
  - Fable `f54f286d-511d-4ec5-aa8d-a560b9435de2`：Request changes，fixed-time expiry
    已修；Fable `96d866b7-5f87-4e4d-92e4-b1eb2fab7b3d`：對 d545 與 bdf 均 Approve，已完成。
  - GitHub code-head checks：completed green；Luna tasks：completed。
- exact stopped stage：Gate 5 已 merge；Gate 6 engineering checkpoint code 在
  `bdf1177495ca6364e2a1bb22915482a76b2d8e97` 完成、pushed、reviewed、CI-green；停止於 Gate 6
  Production Acceptance external evidence collection，尚未完成 P1／P2／P3／P4／P5／P6／P7／
  P8／P9 closure。PR 保持 Draft，不 merge。
- 05:00 更新者與時間：root，`2026-07-16 05:00:12`（UTC+8）。

### 05:00 最終更新 checklist（已完成 read-back）

- [x] Git `HEAD` 與 PR head：`bdf1177495ca6364e2a1bb22915482a76b2d8e97`；branch
      `codex/gate6-production-acceptance`
- [x] Gate 6 PR：[#32](https://github.com/zzw09773/ANILA/pull/32)，`OPEN / MERGEABLE / Draft`
- [x] Code-head required checks：23/23 `SUCCESS`、`notGreen=[]`；`gh pr checks 32` exit 0
- [x] Fable5 final incremental review：session
      `96d866b7-5f87-4e4d-92e4-b1eb2fab7b3d`、exact head
      `bdf1177495ca6364e2a1bb22915482a76b2d8e97`、verdict **Approve**（無 blocker；INFO notes
      見上方 provenance）
- [x] Argus／Fable／Luna／GitHub／Docker stopped state、exact stopped stage 與 05:00 decision
      已記錄；未完成的 P0–P9 blocker 與 owner／resume action 見下節。

## 下一接手優先順序與 owner

1. **Release／maintainer：先不要 merge**；PR #32 保持 Draft，先完成外部 acceptance evidence。
2. **P0 owners（system/data/PKI/security/operations）：** 準備 production signed profile、
   五方簽章與 trust ceremony；不得以 synthetic test signature 代替。
3. **Ops／Security：** 完成 P1 restore/RTO/RPO、P2 fault drills、P4 signed release envelope、
   P9 runtime admission／network deny／usage reconciliation production evidence。
4. **QA／各 workflow owners：** 依事前凍結的 P0 workflow matrix 跑 P5 全鏈與逐筆證據。
5. **獨立具名人類 reviewer／法務採購／卡片與身分驗證 owners：** 分別完成 P6、P7、P8 的
   signed review、書面裁決與 production-equivalent 實體卡／reader／HiPKI 矩陣。
6. **觀測 owner：** P3 必須完成 signed load profile 下連續 7 日 time-lock observation，
   並保留 Sev-1／Sev-2 未處置證據。

## 禁止事項

- 不得 merge Gate 6、不得把 draft 改 ready、不得宣告 Production Go。
- 不得把 schema／CI／unit test 綠燈寫成 P0–P9 acceptance；不得把 Gate 5 merge
  `bca5af0…` 寫成 Gate 6 head。
- 不得提交或產生 production key、trust store、signature、signed production profile、
  token、CA private key 或其他憑證私鑰。
- 不得把 AI、Claude、Fable5 或單一工程師當成 P6 獨立具名人類 reviewer，也不得代替
  五方人員簽核或法務裁決。
- 不得因外部 blocker 尚未完成而事後降低 P0 profile、P3 觀測窗、P5 樣本數或任何
  acceptance threshold。

## Exact resume commands

以下命令從本 worktree 開始；已知 branch、Gate 6 current head 與 PR #32，先以 read-back
確認它們仍一致。涉及 production secret、正式 restore 或 live deployment 的命令不在此
handoff 內。

```powershell
cd C:\Users\USER\.codex\worktrees\ANILA-gate6
git fetch origin main codex/gate6-production-acceptance --prune
git status --short --branch
git branch --show-current
git rev-parse HEAD
git show --no-patch --format=fuller bdf1177495ca6364e2a1bb22915482a76b2d8e97
git log -5 --oneline --decorate
git diff --stat bca5af0cc091dac231903d5b9cfa3dc66d2fa95d..bdf1177495ca6364e2a1bb22915482a76b2d8e97
git diff --name-status bca5af0cc091dac231903d5b9cfa3dc66d2fa95d..bdf1177495ca6364e2a1bb22915482a76b2d8e97
git diff --check
```

PR #32／current head `bdf1177495ca6364e2a1bb22915482a76b2d8e97` 的 exact read-back：

```powershell
gh pr view 32 --json number,url,state,isDraft,headRefName,baseRefName,headRefOid,mergeable,reviewDecision,statusCheckRollup
gh pr checks 32
gh run view 29448123678 --json databaseId,status,conclusion,headSha,workflowName,jobs
```

目前 code-head 的 23 個 checks 已全綠；若需重新確認 current run read-back，可執行：

```powershell
gh run watch 29448123678
gh pr checks 32 --watch
```

重跑 Gate 6／Gate 5 focused policy contracts：

```powershell
$env:PYTHONPATH='packages/anila-security/src'
python -m pytest packages/anila-security/tests -q
python -m pytest infra/policy/tests/test_gate6_p9_enabled_callsite_inventory.py infra/policy/tests/test_gate5_model_governance.py -q
python -m pytest infra/policy/tests/test_gate6_repository_posture.py -q
```

重跑 backup contracts 與 disposable Docker restore smoke（Docker 必須可用；測試預設會
自動移除 disposable container）：

```powershell
python -m pytest infra/deployment/tests/test_production_backup_automation.py -q
$env:ANILA_RUN_DOCKER_RESTORE_SMOKE='1'
python -m pytest infra/deployment/tests/test_production_backup_automation.py -q -k disposable_docker_restore_smoke
Remove-Item Env:ANILA_RUN_DOCKER_RESTORE_SMOKE -ErrorAction SilentlyContinue
docker ps -a --filter "name=anila-backup-test-source-" --format '{{.Names}}'
docker volume ls --format '{{.Name}}' | Select-String 'anila|restore|backup'
```

若需重跑 final static checks，使用 workflow 同等範圍並把結果標成實際 output，不要
預填綠燈：

```powershell
python -m ruff check infra/ci/check_gate6_repository_posture.py infra/policy/tests/test_gate6_repository_posture.py infra/policy/gate6/generate_p9_enabled_callsite_inventory.py infra/policy/tests/test_gate6_p9_enabled_callsite_inventory.py packages/anila-security/src/anila_security/production_acceptance_profile.py packages/anila-security/tests/test_production_acceptance_profile.py
python -m compileall -q infra/ci/check_gate6_repository_posture.py infra/policy/gate6/generate_p9_enabled_callsite_inventory.py packages/anila-security/src/anila_security/production_acceptance_profile.py
git diff --check
```

回報／handoff 更新時，保留 exact head SHA、PR state、CI head、review session／verdict、
running/stopped PID／session／stage 與實際 blocker；若未來狀態尚無證據，明確標記為未確認，
不要推測。
