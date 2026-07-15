# Gate 6 Production Acceptance durable handoff（INCOMPLETE / NO-GO）

## 2026-07-16 立即交接更新

- PR [#32](https://github.com/zzw09773/ANILA/pull/32) 維持 Draft／No-Go；branch `codex/gate6-production-acceptance`，已推 head `cbf37a0`。
- 本輪 commits：`881cb1a`、`97e3860`、`fb672ad`、`cbf37a0`。
- 驗證：ingestion full `248 passed / 10 skipped`；deployment `178 OK / 2 skipped`；model-lock focused `20/20`；PostgreSQL Docker `3 passed`。
- Claude Fable session `d66bae15-cd88-4581-ae53-564aa67549fc` 只審到 `bca5..f2a44e2` 並給 Approve；**不是 current-head 最終審查**。最新 CI 與 current-head Fable 5 max review 尚待完成。
- Gate 6 外部 blockers：P0 真實 prod 與五方簽核；P1 production-equivalent timed restore；P2 實際故障注入；P3 七日 SLO；P4 簽章 release/SBOM/CA/clean host；P5 完整矩陣與 trace；P6 獨立具名人類簽核；P7 法務採購；P8 實體卡/reader/HiPKI；P9 剩餘 runtime/network/usage reconciliation。
- Resume：`cd C:\Users\USER\.codex\worktrees\ANILA-gate6`，再跑 `git status`、`gh pr checks 32 --watch`；之後以 `claude-fable-5`、effort `max`、auto mode resume 上述 session，allowed `Read,Grep,Glob,WebFetch,WebSearch`，disallowed `Bash,Edit,Write,NotebookEdit`。
- 不得宣告 Gate 6 完成；PR 維持 Draft／No-Go。

更新時間：2026-07-16 05:45（UTC+8；05:45 後現況）

## 先看結論

- **Gate 6 engineering checkpoint 的變更已提交並推送；Production Acceptance 結論仍為 No-Go。**
- Gate 6 branch 目前為 `537417471884c6ff456180e5c5523b27d43a65a8`；draft PR
  [#32](https://github.com/zzw09773/ANILA/pull/32) 為 `OPEN / MERGEABLE / Draft`。
  PR 不得 merge、不得改 ready，也不得據此宣告 Go。
- P0 schema／verifier、disabled template、P9 inventory exporter、backup contract、CI
  contract 與目前的測試綠燈，只證明 repository-side engineering contract 的部分邊界；
  **不能因此宣告 P0、P1、P9 pass，更不能宣告 P0–P9 全數通過或 production Go。**
- Gate 6 的 P3 七日觀測、P6 獨立具名人類覆核、P7 法務授權、P8 實體卡矩陣，以及
  P0–P9 的五方人員簽核，都是不可由 schema／CI／AI 壓縮的外部 blocker。
- **Gate 6 維持 No-Go。** AI／Claude／Opus／Fable／Luna 不可代替 P0 五方簽章、P3
  連續 7 日 time-lock、P6 獨立具名人類、P7 法務／採購裁決、P8 實體卡／reader／HiPKI，
  也不能把 P1 production-equivalent restore 或 P9 production network evidence 壓縮完成。
- Gate 5 已 merge；這個 checkpoint 只是從 Gate 5 進入 Gate 6 的工程收尾，不是
  Production Acceptance 核准。

## Provenance（可恢復的 repo 狀態）

| 欄位 | 值 |
|---|---|
| Worktree | `C:\Users\USER\.codex\worktrees\ANILA-gate6` |
| Branch | `codex/gate6-production-acceptance` |
| Base | `main`；Gate 5 merge `bca5af0cc091dac231903d5b9cfa3dc66d2fa95d` |
| Gate 6 commits | `6a126870d760c7d34b2632fe642229c3a878f00c`、`d545630f38336f1e99190ef360711aa0df2ea71d`、`bdf1177495ca6364e2a1bb22915482a76b2d8e97`、`83f0d3556b086a56581e82aa908e26842522d59d`、`4650347af2e9beffbc03efe9ee2233457be596af`、`f538a734fcc76a9254713414aac6440c9ef24fc9`、`537417471884c6ff456180e5c5523b27d43a65a8` |
| Current committed HEAD | `537417471884c6ff456180e5c5523b27d43a65a8`（current final head；Fable findings 尚未修） |
| Gate 5 PR | [#31](https://github.com/zzw09773/ANILA/pull/31)；merge commit `bca5af0cc091dac231903d5b9cfa3dc66d2fa95d` |
| Gate 6 PR URL / number | [#32](https://github.com/zzw09773/ANILA/pull/32) |
| Gate 6 PR state / draft / mergeability | `OPEN / Draft / MERGEABLE`；不得 merge 或改 ready |
| Gate 6 pushed head SHA | `537417471884c6ff456180e5c5523b27d43a65a8` |
| Remote | `origin=https://github.com/zzw09773/ANILA.git` |
| Working tree | 本次只更新此 handoff，其他檔案不得修改；current pushed code head 為 `537417471884c6ff456180e5c5523b27d43a65a8` |

目前的 `HEAD` 是 Gate 6 current head，不是 Gate 5 merge；`bca5af0…` 僅是 base。後續
CI／review 必須指向 `537417471884c6ff456180e5c5523b27d43a65a8`；`f538a73…` 是前一個
unittest-compatible code head，不可誤當目前 PR head。

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
- `services/ingestion-worker/src/ingestion_worker/handlers.py` 與其 helpers tests（current
  head `83f0d3556b086a56581e82aa908e26842522d59d`）：P2 image persistence filesystem／DB／RLS／
  chmod failure fail-closed、atomic publish、rollback cleanup 與 backup reconciliation hardening。

## P0–P9 狀態矩陣

| 項目 | 目前狀態 | 已有／尚有的精確缺口 |
|---|---|---|
| **P0** | **PARTIAL；Production Acceptance 不成立** | schema、verifier、disabled template 與五方 signer role／簽章規則已有 code/test；但尚缺由 system owner、data owner、PKI owner、資安、維運完成的 production signed acceptance profile（含實際 topology、feature、資料上限、RTO/RPO、SLO、load、觀測窗、workflow matrix、callsite inventory、revocation／PKI policy、finding rule、impact matrix 等）。不能把 synthetic test signatures 當成 production 五方簽核。 |
| **P1** | **PARTIAL；No-Go** | P1 verifier 已完成 checkpoint；11 focused root pass、P1+P0 agent validation **32 pass**、backup+P1 **54 pass / 1 skipped**、posture pass。expected／actual 同屬同一 report，且未綁 signed backup manifest，因此只能作 checkpoint，**P1 仍未 pass**；尚缺 production-equivalent restore、P0 RTO/RPO 內計時、DB／blob／artifact／active vector generation checksum／referential integrity，以及 RLS、compartment、revocation 後讀取 negative tests 的完整 drill。 |
| **P2** | **PARTIAL；No-Go** | current head 已補 image persistence fail-closed 與 88 focused regression tests；仍尚缺 Redis／worker／CSP、磁碟滿、網路中斷等 production fault drills、lease/retry 終態、RPO/RTO reconciliation 與完整 production evidence。 |
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
| Gate 6 repository posture（current tree） | **10 tests PASS** |
| Production backup automation | **27 passed / 1 skipped** |
| Combined sequential Gate 6／Gate 5 suite（Fable fixes 後） | **131 passed / 1 skipped** |
| Disposable Docker restore smoke（另跑） | **1 passed** |
| Ingestion-worker P2 focused regression | **88 passed** |
| Ingestion-worker full suite | **235 passed / 10 skipped** |
| P1 verifier focused root validation | **11 passed** |
| P1 + P0 agent validation | **32 passed** |
| Backup + P1 validation | **54 passed / 1 skipped**；posture PASS |
| 465 deployment checks | **failed**：新 test 以頂層 `pytest`／unittest discovery 執行，但環境未裝 pytest；已由 Luna 改為純 unittest |
| f538 focused deployment unittest | **5 passed** |
| f538 full deployment discovery | **162 passed / 2 skipped** |
| f538 deployment Ruff | **PASS** |
| Ruff／compileall／`git diff --check` | **PASS** |
| Gate 6 workflow posture／static checks | **PASS** |
| PR #32 current head `5374174…` CI | **21 pass / 1 CSP pending**（2026-07-16 05:55）；不得宣稱 current PR checks 已綠 |

先前並行 source-scanner 曾因 shared-worktree temporary fixture race 失敗；該競態不是產品
failure，序列重跑的 current Gate 5／P9 suite 為 30 passed，且工作樹／Docker 無殘留。不得
把這些工程 contract 結果擴寫成 P9 runtime network 或 usage 已驗收。

### CI run provenance（歷史 bdf code head；current 83f head 尚 pending）

- Run `29445861023`：因缺少 local `anila_core` install 失敗；已修正。
- Run `29447399506`：Python 3.11 pass；Python 3.12 因 PEP 701 f-string scanner 失敗；
  scanner 已修正。
- Run `29448123678`：前一個 code head `bdf1177495ca6364e2a1bb22915482a76b2d8e97` 的
  23 個 checks 於 `2026-07-16 04:36:01`（UTC+8）read-back 全部 pass；這是歷史工程
  證據，不代表 current head `5374174…` 的 PR checks 已完成。
- CSP full suite job `87463604849`：**success**，耗時 `8m59s`。
- PostgreSQL RLS job `87465627161`：**success**，完成於 `2026-07-16 04:36:01`（UTC+8）。
- Gate 6 Python 3.11 job `87463600938` 已於 `2026-07-15T20:26:06Z`
  （UTC+8 `2026-07-16 04:26:06`）成功完成。

### Editable-install contamination note

曾以 bare `pytest` 執行而載入 ANILA-gate3 的 editable install；該結果不可作為 Gate 6
evidence。接手或重跑 ingestion-worker 時，必須顯式指定 Gate6 source path：
`PYTHONPATH=C:\Users\USER\.codex\worktrees\ANILA-gate6\services\ingestion-worker\src`，
不可依賴未確認的 editable package。

## Opus／Argus provenance

- Current Opus read-only workflow session：`2eb9a4dd…`，**已完成**。
- 該 workflow 提出 P1／P2／P4／P5／P9 的 engineering package；它是工程建議與風險整理，
  **不等於 Production Acceptance，不等於 P0–P9 pass，也不等於 Go**。
- Historical Argus synthesis：session `ec0aae0d-7d6d-424e-905f-e5422b072318`、workflow
  `wf_65cc3531-b18`，10/10 tasks、0 error、頂層 verdict **No-Go**。
- Opus／Argus output 是 read-only engineering／security evidence，**不得把它當 P6 獨立
  具名人類 reviewer，也不得代替五方人員簽核**。

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
- Current Fable5 review session：`cc881337…` 已完成審查 `6ae7f7a…` →
  `83f0d3556b086a56581e82aa908e26842522d59d`，**Verdict: Request changes**。HIGH：handlers
  uuid `.tmp-*` hard crash 會留下孤兒，retry reconcile 只掃 `.bak-*`，retention erase 只按 DB
  final path，可能造成分類 bytes 跨銷毀殘留。MEDIUM：`asyncpg.InsufficientPrivilegeError`／RLS
  被 `_image_persistence_error` 降為 retryable/error，應改為 `StoreError.rls_violation`
  nonretryable critical。LOW：ambiguous backups synthetic EIO 可後續處理。上述 findings 尚未
  覆蓋 P1/fix 的 current final head `5374174…`；Claude PID／process 狀態不寫死，接手者必須
  讀 session JSONL 尾端確認。
- Codex Security diff scan：**no reportable findings**；exact report path：
  `C:\Users\USER\AppData\Local\Temp\codex-security-scans\ANILA\bca5af0_20260715T194201Z\report.md`。
- Fable5、Claude、Argus 與 Codex scan 都是工程／安全證據，不是 P6 獨立具名人類 reviewer，
  也不取代五方人員簽核、法務裁決或實體卡驗證。

## 05:45+（UTC+8）current decision／process snapshot

05:45 後 read-back 確認：Git `HEAD` 與 pushed PR head 均為
`537417471884c6ff456180e5c5523b27d43a65a8`，branch 為 `codex/gate6-production-acceptance`，
PR #32 為 `OPEN / Draft / MERGEABLE`。Current PR checks 為 **21 pass / 1 CSP pending**；
不得宣稱 current head CI 已綠。這份 handoff 只記錄工程與流程狀態，不改 PR、不 merge、不改 Draft。

**Gate 6 正式維持 No-Go。** P0 五方簽章、P3 連續 7 日 time-lock、P6 獨立具名人類、P7
法務／採購書面裁決、P8 實體卡／reader／HiPKI、P1 production-equivalent restore 與 P9
production network／usage evidence 均不可由 AI 或工程測試代替；P2 雖已補 fail-closed code，
仍未完成 production fault evidence。P4／P5 production evidence 亦仍缺。

### Current process／session／stage

- Opus workflow session `2eb9a4dd…`：**completed**；提出 P1／P2／P4／P5／P9 engineering
  package，但不等於 acceptance。
- P1 verifier Luna：**checkpoint 已完成並 push**；11 focused root pass、P1+P0 32 pass、
  backup+P1 54 pass / 1 skipped、posture pass。但 expected／actual 同屬同一 report，未綁
  signed backup manifest，故 **P1 仍未 pass**，不可宣告 production acceptance。
- Fable review session `cc881337…`：**正在審查** `6ae7f7a…` → `83f0d355…`；verdict
  尚未完成。Claude PID／process 狀態不寫死，依下方 JSONL 指令即時檢查。
- GitHub current PR checks：**21 pass / 1 CSP pending**；`5374174…` 的 CI 不得沿用歷史綠燈。
  Fable findings 尚未修，且尚未做 current final-head review。
  結果。Disposable Docker restore smoke 已有 **1 passed**，但不等於 production restore。
- exact current stage：Gate 5 已 merge；P2 image persistence fail-closed code 在 `83f0d35…`，
  P1 checkpoint 在 `4650347…`，unittest-compatible restore tests 在 `f538a73…`，current final
  head `537417471884c6ff456180e5c5523b27d43a65a8` pushed；CI 為 21 pass / 1 CSP pending。
  停止於 **未修 Fable findings、未做 current final-head review**；PR 保持 Draft、Gate 6 No-Go，
  且 P1 因 unsigned backup-manifest binding 限制仍未 pass；
  仍缺 P0／P1／P2／P3／P4／P5／P6／P7／P8／P9 closure。

### Current handoff checklist

- [x] HEAD／branch／PR #32 state 與 current pushed head `537417471884c6ff456180e5c5523b27d43a65a8`
      已 read-back。
- [x] P2 focused **88 passed**、ingestion-worker full **235 passed / 10 skipped**、
      ruff／compile／diff 與 disposable Docker restore smoke **1 passed** 已記錄。
- [x] Opus session `2eb9a4dd…` completed，engineering package 與 No-Go 邊界已記錄。
- [ ] Current PR #32 checks：**21 pass / 1 CSP pending**；不得提前宣稱綠燈。
- [x] P1 verifier checkpoint：11 focused、P1+P0 32、backup+P1 54/1skip、posture pass 已記錄；
      expected／actual 同 report 且無 signed backup manifest，P1 仍標記 No-Go。
- [x] Fable session `cc881337…`：`6ae7f7a…` → `83f0d35…` **Request changes**；HIGH/MEDIUM
      findings 已記錄，尚未覆蓋 current final head `5374174…`。
- [x] P0／P3／P6／P7／P8 及 P1／P2／P4／P5／P9 external blockers、owner 與 resume action
      已列出；仍維持 Gate 6 No-Go。

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
git show --no-patch --format=fuller 537417471884c6ff456180e5c5523b27d43a65a8
git log -5 --oneline --decorate
git diff --stat bca5af0cc091dac231903d5b9cfa3dc66d2fa95d..537417471884c6ff456180e5c5523b27d43a65a8
git diff --name-status bca5af0cc091dac231903d5b9cfa3dc66d2fa95d..537417471884c6ff456180e5c5523b27d43a65a8
git diff --check
```

PR #32／current head `537417471884c6ff456180e5c5523b27d43a65a8` 的 exact read-back：

```powershell
gh pr view 32 --json number,url,state,isDraft,headRefName,baseRefName,headRefOid,mergeable,reviewDecision,statusCheckRollup
gh pr checks 32
gh run list --branch codex/gate6-production-acceptance --limit 5
```

目前 current head checks 為 **PENDING**；不得沿用 `bdf1177…` 歷史綠燈。若需追蹤 current
PR checks，可執行：

```powershell
gh pr checks 32 --watch
```

Fable session JSONL／Claude process 狀態檢查（不寫死 PID）：

```powershell
$claudeProjects = Join-Path $env:USERPROFILE '.claude\projects'
$sessionId = 'cc881337'
$sessionFile = Get-ChildItem -LiteralPath $claudeProjects -Recurse -Filter '*.jsonl' -File |
  Select-String -Pattern $sessionId -List |
  Select-Object -First 1 -ExpandProperty Path
if ($sessionFile) { Get-Content -LiteralPath $sessionFile -Tail 120 }
Get-Process -Name claude -ErrorAction SilentlyContinue
```

重跑 Gate 6／Gate 5 focused policy contracts：

```powershell
$env:PYTHONPATH='packages/anila-security/src'
python -m pytest packages/anila-security/tests -q
python -m pytest infra/policy/tests/test_gate6_p9_enabled_callsite_inventory.py infra/policy/tests/test_gate5_model_governance.py -q
python -m pytest infra/policy/tests/test_gate6_repository_posture.py -q
```

Ingestion-worker 重跑必須顯式使用 Gate6 source；不要直接 bare `pytest`，避免載入
ANILA-gate3 editable install：

```powershell
$env:PYTHONPATH='C:\Users\USER\.codex\worktrees\ANILA-gate6\services\ingestion-worker\src'
python -m pytest services/ingestion-worker/tests/test_handlers_helpers.py -q
python -m pytest services/ingestion-worker/tests -q
```

Deployment restore tests 已改為純 unittest；重跑使用：

```powershell
python -m unittest discover -s infra/deployment/tests -p 'test_*.py'
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
