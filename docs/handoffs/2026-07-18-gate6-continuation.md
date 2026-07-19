# Gate 6 continuation handoff（INCOMPLETE / NO-GO）

更新時間：2026-07-18 07:35 CST

## 1. 新 session 的任務

持續完成：

> `docs/planning/anila-development-roadmap.md`，並依目前實際
> `~/.codex/AGENTS.md`、repo `AGENTS.md` 與 escalation ladder 分派工作。

目前不能把目標縮成「讓測試綠」或「完成 Gate 6 engineering contract」。正式完成仍要逐項
稽核 roadmap 的 Gate 0–7；不能由本機測試替代 production／外部證據。

## 2. 使用者指示與不可違反事項

- 回覆使用繁體中文與台灣用語。
- **主代理禁止自行實作程式碼。** 寫碼由 `coding` 開始；同一 bounded task 兩次失敗後依序
  `striker1` → `striker2` → `striker3` → Claude Rescue。主代理負責調查、整合、驗證與交付。
- 每個 coherent change 先跑 exact `review`（Sol），再跑 `claude-fable-5 --effort xhigh`
  orchestrating Opus 4.8；不能因 timeout／沒 stdout 就判定中斷，必須看 PID、session journal、
  workflow journal 與 result。
- 2026-07-18 P4 的 Codex `review` prompt 連續兩次被平台內容分類器誤擋；使用者已明確指示：
  **「算了，你把測試丟給 Opus 4.8 去看。」** P4 目前直接交給 `claude-opus-4-8 --effort xhigh`
  做只讀 review。
- PR bot feedback 必須獨立驗證，不能看到就接受。
- 目前可用 LLM：`http://172.16.120.35:7000`（`gpt-oss-20b`）。
- 目前可用 embedding：`172.16.120.35:9001`（Triton gRPC，`nv-embed-v2`）。
- 模型未來以 external-first 為主；donkernet 只保留明確列出的少量 internal models。
- external target 同時支援 IP `host:port` 與 FQDN/domain `host:port`；gRPC 必須帶 port。
- production HTTP 模型端點仍必須是 HTTPS。
- HiPKI 先用 CHT synthetic 方法測；實體卡／reader／vendor HiPKI／正式 CA/CRL remain。
- Docker 可用，測試 image／container 不必保留；但 `anila-models-net` 錯誤 shape 不得自動
  delete/recreate，必須 fail-closed 並由操作員處理。
- 不得把 synthetic、engineering、AI review 或 schema verifier 寫成 Production Acceptance。
- 使用者尚未授權本輪 commit／push／PR 更新／ready／merge；全部維持不動。

## 3. Git／PR 權威狀態

| 欄位 | 目前值 |
|---|---|
| Repo | `/home/c1147259/桌面/ANILA/anila-migration-20260706/ANILA` |
| Branch | `codex/gate6-production-acceptance` |
| Local committed HEAD | `5c5a1b2f5e60ff199ebd746935bee2685459a3ee` |
| Remote | `origin=https://github.com/zzw09773/ANILA.git` |
| PR | [#32](https://github.com/zzw09773/ANILA/pull/32) |
| PR state | `OPEN / Draft / MERGEABLE / CLEAN` |
| Remote head | `5c5a1b2f5e60ff199ebd746935bee2685459a3ee` |
| Remote checks | 舊 head 的 23 checks 全 SUCCESS |
| Local worktree | 大量 Gate 5／6 intended changes，包含多個 untracked 新檔；未 commit／未 push |

**重要**：遠端 checks 完全不涵蓋目前本地 diff，不得宣稱 current head CI 已綠。

PR #32 的三個 Gemini review threads 目前都已 resolved，沒有未解 thread。獨立裁決：

1. non-JSON Gate 6 posture gap：有效，已修。
2. duplicate JSON-only posture check：有效，已移除重複。
3. `pg_dump` substring 建議：不成立；reachable command 固定把 `pg_dump` 當獨立 argv 元素，
   substring 反而會誤分類。

## 4. 當前仍在執行的 Opus 4.8 P4 review

使用者要求直接交給 Opus 4.8 後，已啟動 read-only review：

- PID（handoff 時）：`2009989`
- Codex exec session：`34213`（新 session 可能不能直接接這個 exec handle）
- Claude session journal：
  `~/.claude/projects/-home-c1147259----ANILA-anila-migration-20260706-ANILA/7670c884-cf10-403f-a283-867fc5c8828f.jsonl`
- Model：`claude-opus-4-8`
- Effort：`xhigh`
- Exact scope：
  - `packages/anila-security/src/anila_security/release_envelope.py`
  - `packages/anila-security/tests/test_release_envelope.py`
  - `packages/anila-security/src/anila_security/__init__.py` 的 release-envelope exports
- 禁止 edit／commit／push／PR write／destructive command。

續接時先看：

```bash
ps -p 2009989 -o pid,ppid,etime,stat,%cpu,%mem,cmd
tail -80 ~/.claude/projects/-home-c1147259----ANILA-anila-migration-20260706-ANILA/7670c884-cf10-403f-a283-867fc5c8828f.jsonl
```

若 PID 消失，從 journal 最後的 assistant text 讀 verdict；不要因沒有 CLI stdout 就重跑。

## 5. 已完成並完成 review 的 engineering closure

### 5.1 P2 fault-drill verifier：CLEAN（只屬 NON_ACCEPTANCE）

- exact source-derived JUnit test identity、job/step/env/shell/argv contract、lease token bound、P0
  binding、reconciliation 與 full negative mutation matrix 已完成。
- final Sol：CLEAN。
- final Fable workflow：`wf_35539516-601`，10/10 agents completed，structured result CLEAN。
- 這不是實際 production fault injection；redis／worker／CSP、disk-full、network interruption 與
  production RPO/RTO evidence remain。

### 5.2 P5 trace／compartment：Sol + Fable CLEAN（只屬 NON_ACCEPTANCE）

- governance multi-row lock order 已統一為 resource → authority。
- P5 JSON parser 拒絕 NaN／Infinity／-Infinity，固定 redacted error、無 cause。
- 兩條真實 client denial path pin exact
  `{"detail": "trace clearance/compartment 拒絕"}`。
- exact Sol：CLEAN。
- Fable workflow：`wf_3e3633c2-4cc`，五個 Opus 維度完成，最終 `CLEAN`。
- focused CSP 69 passed；P5 evidence 33 passed。
- runtime lock-order LOW 建議經兩名 Opus 仲裁員一致否決：現行程式正確，只有假設未來改壞，
  不是 current reachable finding。
- P0-frozen production workflow matrix、fixed-N production-like e2e trace evidence remain。

### 5.3 P3 non-schema slice：Sol CLEAN，但整體 P3 尚未 CLEAN

Fable `wf_19aa9d9c-91b` 先找到 1 HIGH + 3 MEDIUM + 1 LOW。已由 `coding` 修完不需改
P0 schema 的部分：

- `json.loads` 前的 bounded byte-level JSON scanner；限制 node/container/item，處理 string、
  escape、UTF-8、number、literal。
- 固定、不可由 caller 覆寫的 `missing_external_p3_evidence`：
  - `time_locked_metrics_source_provenance`
  - `incident_system_complete_export_watermark_provenance`
  - `independent_operational_approval`
- forged-authority 測試已真正穿越 authority boundary，會殺 exact-type guard removal。
- 誤稱 six-day 的測試已改成正確的 first-sample boundary alignment。
- mutation/fuzz evidence：關閉 pre-scan 會殺 3 個 controls；2,000 個 random valid JSON 與標準
  parser 一致。
- verification：focused 21 passed / 61 subtests；deployment 259 passed / 1 skipped / 295
  subtests；P0/security 143 passed / 1 skipped；Ruff/format/compile clean。
- exact Sol 結果：`CLEAN FOR THIS NON-SCHEMA SLICE`。

**仍未修的 P3 HIGH**：metrics cadence 目前由 exporter 自報。允許
`interval_seconds=86400` + `tolerance_seconds=86400` 時，七日只需 5 個樣本，42 小時 gap
內的 SLO breach 可消失。Fable 已用 probe 重現。

### 5.4 P4 release-envelope 目前實作狀態：等待 Opus verdict

Coding tier 兩次失敗後已依 ladder 升級 `striker1`。Striker1 attempt 1 完成：

- authority JSON 與 bundle root component-wise descriptor traversal。
- descriptor-based closed-set scan 與 source identity revalidation。
- private `_SnapshotLease` 保存 parent/root descriptor 與 dev/ino/uid。
- result 支援 context manager、`close()`、cleanup alias 與 GC finalizer。
- closed 後拒絕存取 snapshot；path replacement 不刪 successor。
- snapshot dir 0700、file 0600；完成後 closed-set rescan + per-file rehash。
- tests 包含 parent component replacement、same-size/restored-mtime change、inode substitution、
  hardlink、snapshot extra/content injection、hostile umask、mode/uid、context/GC/double close、
  replacement 與 exception cleanup。
- focused 32 passed；anila-security full 167 passed；Ruff/format/py_compile/diff clean。

此前 coding attempt 2 的 manual cleanup snapshot API 有真實 lifecycle 缺陷，不能拿舊的 26／161
結果當 closure。現在必須以 Opus 4.8 的新 verdict 為準。

### 5.5 其他已關閉 engineering 項目

- Gate 1 dotenv/capability/ledger/card scanner：exact Sol CLEAN。
- Gate 4 T3 proxy stream：exact Sol CLEAN。
- Studio Redis durability：595 passed，exact Sol CLEAN。
- S4 finding 已推翻。
- R3 legacy dispatch guard：exact Sol CLEAN。
- Gate 6 G6b artifact rollback mutation：focused 74，exact Sol CLEAN。
- R5 strict mypy：62 files / 0 errors；256 non-live tests，exact Sol CLEAN。

## 6. 指定模型與 CHT 本輪 live smoke

2026-07-17 重新實測（均為 engineering smoke）：

| 項目 | 結果 |
|---|---|
| `7000 /v1/models` | `gpt-oss-20b` |
| `7000 /v1/chat/completions` | exact `GATE6_LIVE_OK` |
| `9001` via temporary embedding proxy | health `ok`；model `nv-embed-v2`；4096 finite values |
| CHT Docker synthetic | localhost-only；pkcs11 ret 0；3 certs／2 CA；正確 PIN sign ret 0；錯誤 PIN ret 1 |
| CHT/card focused suite | 66 passed / 9 warnings |

臨時 Docker smoke containers/networks 已清除。

`anila-models-net` 目前 live shape 仍是：

```text
Driver=bridge
Internal=false
Containers=[]
```

工程 helper 正確要求 `bridge + Internal=true` 並 fail-closed。這是 deployment state blocker，不是
程式 bug；不要自動刪除／重建 network。

## 7. Gate 5 必須保持可見的真實 blocker

Gate 5 雖歷史上已 merge，但 R6 immutable one-capture 在 live `7000` 的 current evidence 是：

- 160 frozen cases。
- policy bypass：0/20。
- false dispatch：0/90。
- routing top-1：**53/60（88.3%）**，未達 roadmap 要求 ≥95%。

這是實際 blocker，不能重抓 dataset、改 denominator 或隱藏。後續只能修 routing behavior／prompt／
decision path，然後用同一 frozen capture 重評。

## 8. Gate 6 P0–P9 正式狀態

| 項目 | 正式狀態 | 不可由本機工程測試取代的 remain |
|---|---|---|
| P0 | PARTIAL / No-Go | 五方真實 production signed profile |
| P1 | PARTIAL / No-Go | production-equivalent timed restore、RTO/RPO、完整 checksum/RLS/compartment/revocation |
| P2 | engineering CLEAN / Production No-Go | 真實 fault injection、reconciliation 與 RTO/RPO evidence |
| P3 | engineering incomplete / Production No-Go | signed cadence HIGH；完成後仍需連續七日 time-locked observation |
| P4 | engineering review in progress / Production No-Go | 真 release-owner trust/signature、production SBOM、clean-host air-gap deploy、runtime readback |
| P5 | engineering CLEAN / Production No-Go | P0-frozen matrix、fixed-N production-like e2e sample evidence |
| P6 | BLOCKED external | 不同於修補者的獨立具名人類 reviewer + signed report |
| P7 | BLOCKED external | 法務／採購書面授權裁決 |
| P8 | BLOCKED external | 實體卡、reader、vendor HiPKI、正式 CA/CRL、OS/browser/session matrix |
| P9 | PARTIAL / No-Go | production packet capture/deny、usage reconciliation、五方簽核 |

Gate 7 進入條件是 Gate 6 通過 + single-agent 穩定一個 release + 另行 Go/No-Go；目前不得開始或
宣稱 Gate 7 完成。

## 9. P4b integration 的已知架構邊界

現有外網 exporter bundle **不含 CA/CRL**；內網 deploy 在 bundle 選定後才由操作員取得／生成
CA/CRL。因此不能讓 exporter 虛構 `ca_bundle.sha256`。

安全選項只有：

1. 擴大 air-gap bundle contract，把公開 CA/CRL 納入外網 closed set；或
2. 在內網 CA/CRL finalization 後完成最終 envelope verification。

在未決定前，只能做 fail-closed admission/readback hook；不得產生 production signing key、不得用
repo CSPKI 或 CI fixture 冒充 production CA provenance。

## 10. 下一步（精確順序）

1. **收 P4 Opus 4.8 verdict**（見 §4）。
   - CLEAN：再依 AGENTS 跑 P4 Fable/Opus final gate。
   - 有 finding：回 `striker1` attempt 2；若第二次仍失敗，升 `striker2`，附完整 failure trail。
2. **P3 signed cadence schema task**（P4 review 完成後才寫，避免碰同一 release fixture）：
   - `production_acceptance_profile.py` 的 signed `observation_window` 新增必填
     `cadence: {interval_seconds, tolerance_seconds}`。
   - bounds：interval 1..86400；tolerance 0..interval。
   - P3 metrics cadence 必須 exact match signed cadence，不能由 exporter 放寬。
   - 更新所有 P0 fixtures：production profile tests、P3、fault drill、restore drill、P9、
     release-envelope tests。
   - 每個 P0 mutation 必須重新簽章，避免只打到 signature mismatch；補 cadence mismatch、missing、
     unknown、0、>86400、tolerance>interval 與 guard-removal mutation tests。
   - 由 `coding` 開始，不由主代理寫碼。
3. P3 cadence change 完成後跑 exact Sol，再跑 Fable/Opus；不得只沿用 non-schema CLEAN。
4. P4 engineering closure 後，再決定最小 admission/readback integration；production CA/CRL remains。
5. 回頭處理 Gate 5 R6 53/60 blocker，必須沿用 frozen capture。
6. 跑 current local diff 對應的 full suites、Docker/compose checks、7000/9001/CHT smoke。
7. 對 **exact full PR diff** 跑 Sol，再跑 Fable/Opus；核對 GitHub checks/thread 狀態。
8. 更新 roadmap 與 2026-07-16 handoff 的 2026-07-18 checkpoint，清楚區分 engineering closure 與
   production remain。
9. 未取得使用者明確授權前，不 commit、push、改 PR ready、留言、resolve 新 thread 或 merge。

## 11. 測試暫存物

`/tmp` 目前仍有 29 個舊 `anila-release-envelope-*` 測試目錄，合計約 172 KiB；部分權限為 000，
來自 striker1 之前的 snapshot lifecycle tests。新 lease 應自動清理，但這些舊 artifact 不會自動消失。

依 repo 規範，刪除資料前先取得使用者同意；新 session 不要直接 `rm -rf`。

## 12. 新 session 開始時的最小檢查

```bash
cd /home/c1147259/桌面/ANILA/anila-migration-20260706/ANILA
cat ~/.codex/AGENTS.md
cat AGENTS.md
git status --short --branch
git rev-parse HEAD
gh auth status
gh pr view 32 --repo zzw09773/ANILA \
  --json number,url,state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup
ps -p 2009989 -o pid,ppid,etime,stat,%cpu,%mem,cmd
tail -80 ~/.claude/projects/-home-c1147259----ANILA-anila-migration-20260706-ANILA/7670c884-cf10-403f-a283-867fc5c8828f.jsonl
```

本 handoff 是 current continuation SSOT；舊 `2026-07-16-gate6-production-acceptance-incomplete.md`
仍有歷史價值，但其中 current claims 已落後。
