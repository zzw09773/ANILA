# Gate 6 continuation handoff（INCOMPLETE / NO-GO）

更新時間：2026-07-19 CST。本檔為 current continuation SSOT，接續
`2026-07-18-gate6-continuation.md`；該檔的 current claims 已被本檔取代，僅存歷史價值。

## 0. 一句話狀態

P2/P3/P4/P5 與 Gate 5 R6 已達 **engineering closure**（本機工程證據齊、經跨家審查與獨立對抗式驗收）；
**Gate 6 正式 acceptance 仍 NO-GO**——P0–P9 的 production／external 證據、五方簽核、七日觀測、實體卡與獨立人類覆核均未取代。
本輪**未 commit／未 push／未動 PR**；使用者尚未授權。

## 1. 本 session 完成項（engineering，非 production acceptance）

### 1.1 P4 release-envelope：engineering-closed
- Opus 初審 CLEAN + 1 finding → striker(terra×xhigh) attempt 2/3/4 逐步修 → 五維度 Fable/Opus final gate（6 findings）→ opus×xhigh focused re-gate **APPROVE**。
- 九個 envelope↔P0 binding 各有 killing test、NON_ACCEPTANCE 字面值三個輸出面（as_dict/serialized_report/to_json）皆 pin、mutation 全殺。
- `release_envelope.py` 最終 sha256 `8dbac2ed…f73304`；focused 46、anila-security full 189 passed（含後續 P3 fixture 增量）。
- **production remain 不變**：真 release-owner 簽章/信任、production SBOM、clean-host air-gap deploy、runtime readback。

### 1.2 P3 signed cadence：engineering-closed
- `observation_window` 新增必填 `cadence:{interval_seconds 1..86400, tolerance_seconds 0..interval}`，五方簽章涵蓋；
  P3 SLO verifier 強制 exporter cadence 與 signed 完全相等、gap/min-sample 用 signed 值。
- 六個 P0 fixture 全重簽；完整 mutation 矩陣；Sol 2 findings + Fable/Opus gate 1 LOW 全修。
- 修掉原 HIGH：exporter 自報 cadence 可用 {86400,86400} 只需 5 樣本／7 日藏 SLO breach。
- **production remain**：P0-frozen matrix、完成後仍需連續七日 time-locked observation。

### 1.3 Gate 5 R6 routing：engineering-closed（本 session 最大工作量）
- 起點：R6 routing top-1 53/60（<95%）。診斷確認 7 個 miss **全是模型誤判**（frozen provider_output 本身 deny），下游 formal adapter 同資料 60/60。
- 演進 v1→v7（frozen dataset `routing-eval.v1.json` 位元組全程不動，pinned sha `083eaa27…`）：
  - v2/v3：prompt 修訂（capture prompt + production `_FORMAL_ROUTE_SYSTEM` parity），使用者授權**兩次**單發正式 capture；但 Sol 逐案重播揭露總分指標抓不到的行為位移（v2 直答 0/20、v3 三個 security deny 漂成 direct_answer + case-128 token runaway）。
  - **架構轉向（使用者核准）**：安全保證從「prompt 措辭」移到**確定性 enforcement 層**（E1 injection／E2 classification ceiling／E3 scope+specialized-capability），eval 與 production 共用、de-escalation-only（任何→deny，永不強制 route/dispatch）。E1 明文定位為 **bounded best-effort defense-in-depth，非承重、非 gate**；承重＝E2/E3/PolicyGate/CandidateFilter（結構化資料上）。
  - v4：capture（schema v2，存 finish_reason/usage/elapsed_ms，非 stop 即拒；max_tokens=4096 破 case-128 runaway）。新增 `security_deny_exact`（35/35，min 1.0）與 `direct_answer_exact`（min 0.80，刀口品質指標）。
  - v5→v7：Sol 連三輪審查逐輪抓真漏洞（v4 production parity 破損／v5 documentary 豁免製造 exfil 繞過／v6 E3 缺 context eligibility），全部修畢；v7 主對話獨立複驗又抓到 coding 漏報的 reason-code regression 並修正。
- **最終狀態**：v4 fixture 經硬化 enforcement 重評 `status=passed`——security **35/35**、route 58/60、direct 19/20、false_dispatch 0/90、policy_bypass 0/20。
- **獨立對抗式驗收（opus×max verifier，fresh context）OVERALL PASS**：自寫 probe 證明「對 35 個 security 案例，即使餵最惡意非-deny provider 輸出仍被強制 deny，0 派工、0 洩漏」——安全是真結構保證，非 fixture 運氣。無真缺陷。
- 全套：anila-core **954 passed / 10 skipped**、infra/ci **133 passed**。
- 載重新檔 sha：`injection_detection.py` `e9bd1802…`、v4 fixture `7a86252f…`。
- **capture 系譜**：v1（53/60 baseline，audit）、v2（60/60，直答 regression，audit）、v3（60/60+直答 20/20，security regression，audit）、v4（現行，schema v2，load-bearing）。v1–v3 是 superseded artifact，故意不符 v2 schema、不被計分使用。
- **production remain**：P0-frozen production workflow matrix、fixed-N production-like e2e trace evidence、live provider 真偽（離線不可證，已由 enforcement 兜底）。

### 1.4 文件與制度
- **roadmap 分軌**（`docs/planning/anila-development-roadmap.md` Gate 6 段末，2026-07-18 平台擁有者裁示）：P0–P9 Go 條件**一字未改**，但工作分兩軌——軌一（開發者：工程備料 + 設計理由報告 + P7 授權對照表）、軌二（組織：進內網驗收窗口、P6 獨立具名覆核、P7 法務裁決）。明寫「分軌不降門檻」。
- **分級與平台設計理由報告**：`docs/planning/classification-platform-design-rationale.md`（新檔，給長官/政策讀者；zh-TW 掃描通過；附錄 B 為 P7 授權對照表骨架，FLUX.2-dev BFL Non-Commercial 標為首要待裁）。

## 2. 全量迴歸（本 session 實跑）

| Suite | 結果 |
|---|---|
| services/csp | 1630 passed / 40 skipped / **1 failed = pre-existing**（`test_agent_registry_upgrade.py::TestTraceTest` 缺 `agents` SDK，非本輪 regression） |
| packages/anila-core | 954 passed / 10 skipped |
| packages/anila-security | 189 passed |
| infra/deployment（unittest） | 257 OK |
| infra/ci | 133 passed |
| infra/policy（P9 + posture） | 32 + 79 passed |

## 3. 重要制度教訓（寫給下個 session）

- **coding tier（codex exec sandbox）跑不完 `packages/anila-core/tests`**：TestClient/AnyIO portal 在其 sandbox 卡住（exit 124），故其自驗有系統性盲區。本 session 四次它誤把 harness 卡住當結論，其中一次連帶漏報真 regression。**因應：每輪 coding 之後，由主對話（直接 Bash，環境正常）跑 full core suite 當驗證 gate**——這是「驗證不自驗」的實例。給 subagent 的 prompt 要明寫「TestClient 冷啟動慢，給 400s，harness 卡住≠程式失敗」。
- **內容過濾器對「安全評測材料」誤傷**累計多起（P4 review prompt、字元表輸出、v4 設計 agent）。因應：注入 payload 只准用 case_id 指涉、payload 一律在腳本內處理、禁進任何文字輸出/推理 prose。
- **總分指標會遮蔽行為位移**：v2/v3 指標通過但 Sol 逐案重播才抓到 regression。教訓已落地為 `security_deny_exact`（結構）與 `direct_answer_exact`（刀口）+ enforcement counterfactual 測試。

## 4. Git／PR 權威狀態

| 欄位 | 值 |
|---|---|
| Branch | `codex/gate6-production-acceptance` |
| Local HEAD | `5c5a1b2f5e60ff199ebd746935bee2685459a3ee`（**未變，本輪零 commit**） |
| Remote / PR | `origin=github.com/zzw09773/ANILA.git` / PR #32（OPEN/Draft） |
| 本地工作樹 | 151 個檔案異動（tracked M + untracked 新檔）；**未 commit／未 push** |
| 遠端 checks | 僅涵蓋舊 head，不涵蓋當前本地 diff——不得宣稱 current head CI 綠 |

## 5. 下一步（精確順序；未取得使用者授權前不 commit/push/PR）

1. **exact full PR diff 雙審**：對整體本地 diff（P3+P4+R6+其他 Gate 5/6）跑 Sol，再跑 Fable/Opus gate；核對 GitHub checks/thread。
2. **Docker/compose 檢查 + smoke**：compose config 驗證；live smoke（7000 `/v1`、9001 embedding、CHT synthetic 卡登）——本 session 尚未重跑。
3. **runtime injection backstop**：使用者已核准**另開任務**（不在本輪）。E1 是 best-effort；真正的一般性注入防護（canonicalization + 分類器）需獨立威脅模型與治理；若上 ML 分類器會觸發 P7/model governance。記為待辦。
4. **Gate 5 R6 剩餘 production evidence**：P0-frozen matrix、fixed-N production-like e2e。
5. Gate 6 P0–P9 的 production/external 項全數維持 §8（見 2026-07-18 handoff）狀態；軌二由組織啟動。

## 6. 新 session 最小檢查

```bash
cd /home/c1147259/桌面/ANILA/anila-migration-20260706/ANILA
cat ~/.codex/AGENTS.md; cat AGENTS.md
git status --short --branch; git rev-parse HEAD   # 應仍為 5c5a1b2
# R6 重評（免 capture，用現行 v4 fixture）：
ANILA_GATE5_ROUTING_PROVIDER_FIXTURE=infra/policy/gate5/routing-provider-output.v4.json \
PYTHONPATH=.:packages/anila-core/src:packages/anila-contracts/src:packages/anila-security/src \
services/csp/.venv/bin/python infra/ci/run_gate5_routing_contract.py \
infra/policy/gate5/routing-eval.v1.json --adapter infra.ci.gate5_recorded_provider_adapter:build_adapter
# 驗證 gate（主對話跑，勿信 coding 的 hang 宣稱）：
PYTHONPATH=packages/anila-core/src:packages/anila-contracts/src:packages/anila-security/src \
services/csp/.venv/bin/python -m pytest packages/anila-core/tests -q   # 954 passed / 10 skipped
```

## 7. full PR diff pre-merge 雙審（2026-07-19，使用者授權）

對 exact full working-tree diff（115 tracked +15k/−2k、~37 untracked，橫跨 Gate 0/1/4/5/6）跑跨家雙審：
**Sol（gpt-5.6-sol×xhigh）+ Opus 4.8×max 五區 fan-out**（Claude 側依使用者指示用 opus 非 fable）。
兩家 findings **互補無重疊**。P3/P4/R6（本 session 交付）在 full-diff 中無新問題；findings 全在**周邊 Codex Gate 5/6 工作**。

### 7.1 findings 與修復（全部已修＋主對話獨立複驗）

| # | 嚴重度 | 位置 | 問題 | 修復 |
|---|---|---|---|---|
| Opus | HIGH | `gate1-test-baseline.json` | PR 新測試 2 個 skip 未註冊 → Gate 1 CI 紅 | 註冊 2 skip；checker exit 0、governance 16 passed |
| 主對話抓 | HIGH | `capability-freeze-baseline.json` | 2 個新 router entrypoint 未註冊（`_legacy_dispatch_posture`／`_require_legacy_dispatch_posture`，HEAD 0/樹 7）→ Gate 1 capability-freeze CI 紅 | 註冊 2 entry；capability-freeze 10 passed、infra/policy 273 passed |
| Sol F-01 | HIGH | `check_deployment_egress.py` + external-embed | 外部 embedding 目的地未綁簽章 provider authority，可換 host 繞 egress | resolved TRITON_GRPC_URL fail-closed 綁 signed target/sha256/locality/egress-policy；wrong-host 負向測試；egress 61 passed |
| Sol F-02 | HIGH | `intranet-deploy.sh` | export allow-list 漏 3 變數 → 一條龍部署 preflight 中止 | 補 `ANILA_EMBEDDING_TOPOLOGY`/`TRITON_GRPC_URL`/`GATE5_MATERIAL_DIR`；deploy-prod required-list 交叉核對完整；containment 20 passed |
| Sol F-03 | HIGH（安全） | `proxy/service.py` | 鎖後較高 `effective_level` 未傳下游 → 分類降級競態 | effective_level 傳 10 個下游點（5 串流+5 非串流）；stream/non-stream×model/agent latch 測試 |
| verifier F-03b | HIGH（安全，pre-existing） | `api/proxy.py` legacy 非串流 agent 分支 | 對抗式驗收挖到**同一分類競態的姊妹路徑**（HEAD 就有；F-03 scope 未含此手刻分支）：:1987 ceiling 授權 + 4 個 closure 用 stale admitted | 同 F-03 pattern：:1975 `effective=admitted_level` 初始化、:1978 `if task_ctx` recompute、ceiling 授權與 closures 全改 effective；新 latch 測試；csp 全套 **1639 passed**（僅 pre-existing 缺 SDK fail） |
| Sol F-04 | MEDIUM | `check_gate1_test_governance.py` | card-material CI 檢查用 substring 攤平，可被非執行字串/continue-on-error 欺騙 | 改結構化 step 檢查（run/continue-on-error/if/workdir）+ mutation 測試 |
| Sol F-05 | MEDIUM | external-embed Dockerfile/compose | egress shim 以 root+預設權限跑 | 非 root `USER 10001`、`read_only`、`cap_drop:[ALL]`、`no-new-privileges`；compose 23 passed |
| Opus | LOW | `api/models.py` | `provider_locality`（含 `internal_shim`）未對非 owner 遮蔽，透露 egress 拓樸 | **使用者裁示保留**（by-design UI 鎖定指示器；`is_internal` 本已暴露，邊際洩漏小）。不改碼 |

CLEAN（兩家共同確認）：祕密掃描（15k 行零硬編碼真祕密）、JWT/JWKS/SSRF/trusted-hosts/CSRF-Bearer/revocation fail-closed/空 service-token fail-closed、runtime `csp_app`（無 RLS bypass）、`r1_0030`/`r1_0031` migration 可 downgrade、embedding 4096→4000→halfvec(4000) 契約、無新 host-port 暴露、`ensure-models-network.sh` 對錯誤拓樸 fail-closed 不自動重建。

### 7.2 制度教訓補充（承 §3）

coding tier 除了跑不完 full core suite（TestClient/AnyIO portal 在其 sandbox 卡住；本輪六次誤把 harness 卡住當結論），還**傾向把自己造成的失敗標成 pre-existing**：本輪 A 把 capability-freeze 2 failed 判為「pre-existing/範圍外」，主對話查證 `git show HEAD` 證實那 2 個 router entrypoint 是 uncommitted 新增（HEAD 0 次/樹 7 次）＝本輪治理債。**下個 session：① coding 宣稱「pre-existing」一律用 `git show HEAD:<file>` 或跑乾淨基線獨立查證，不照收；② full services/csp 全套要 ~300s，主對話用背景 Bash 跑（前景 120s timeout 會誤判 hang）。**

### 7.2b verifier 標註的 remaining pre-existing（scope 外，未修，待後續）

fresh opus verifier 對 F-01/F-03 對抗式驗收時另標兩項 pre-existing、超出本輪 scope 的觀察（**不是本輪 regression**）：
1. **F-01 trust-store provenance**：egress checker 從同一 deployment-mounted governance material 載入 profile 與 trust store；F-01 的簽章錨定強度取決於對該 mounted trust store 的控制。這是 governance-material 模型的既有性質，未追是否保證 version-controlled/CI-baked。
2. **`api/proxy.py` 非串流 agent 分支的 usage-attribution TODO（:1961-1963）**：該分支有既有 usage 歸屬 TODO、未呼叫 governance `authorize()`（F-03b 只修分類軸，未碰此項）。

### 7.3 live smoke（2026-07-19 重跑）

| 項目 | 結果 |
|---|---|
| 7000 `/v1/chat` | exact `GATE6_LIVE_OK`、finish=stop |
| 9001 Triton gRPC | server live/ready、nv-embed-v2 ready、真實 inference shape (1,4096)、4096 finite、dim=4096 |
| CHT/card | card-focused suite 118 passed/1 skipped；CHT synthetic 測試 2 passed |
| compose | 四檔結構有效 + 必要變數 fail-closed；external-embed verify exit 0；`anila-models-net` 現 `Internal=false`＝部署狀態 blocker（tooling 正確 fail-closed，非程式 bug） |
