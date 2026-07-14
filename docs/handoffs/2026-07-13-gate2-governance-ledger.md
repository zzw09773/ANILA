# Gate 2 governance ledger 接手紀錄（2026-07-13）

> 狀態：**closeout candidate**。功能、full suite、true PostgreSQL、Docker 與
> HTTP Full Trace 已在本機通過；Aegis、Sentinel、Parfit 均已 Approve。GitHub
> final-head CI 與指定 `claude-fable-5` 終審仍是 merge gate。

## 1. 精確接手點

- Repository：`zzw09773/ANILA`
- PR：[#27](https://github.com/zzw09773/ANILA/pull/27)
- Branch：`feat/gate2-governance-ledger`
- Base：`main` / `369f315df12059433fa47e9d1005c0e779365197`
- 已驗證 code head：`736a02152b4742d64a26ad29981543db4a4af10a`
- 本文件後續的 closeout commit 只更新接手證據；公司端以 PR remote tip 為準。
- SSOT 仍是 `main`；不得直接在 downstream branches 間互相 merge。

公司端接手：

```powershell
git fetch origin --prune
git switch feat/gate2-governance-ledger
git pull --ff-only
git status --short --branch
gh pr view 27 --repo zzw09773/ANILA --json headRefOid,isDraft,mergeable,statusCheckRollup
```

## 2. 本輪完成內容

Gate 2 的核心不是只有 schema，而是把治理要求推進到執行邊界：

- `anila-contracts` v1 wire contracts 與遞迴不可變 payload。
- classification reconciliation、canonical clearance、compartment/RLS、one-way latch。
- SourceSnapshot/Citation sealing、RAG 與 memory data ceiling、raw blob/外部 judge 重驗。
- durable Task/TaskRun、usage、PolicyDecision、AuditEvent 與 TraceSpan closure。
- break-glass JWT/AuthSession 綁 owner/ticket/expiry，migration `r1_0019` 撤銷舊 family。
- artifact write 需要同一 Task/TaskRun/requester 下具名 `artifact_tool` 與 `artifact`
  egress 授權。
- Router/FLUX 逐 agent service identity、request-scoped trace、stream cancellation usage
  保留與 300 秒／10,000 events／16 MiB 上限。
- signed pilot profile v3 綁 exact callsite、registry target、data ceiling、有效期與
  `deployment_artifacts.csp_image_id`；runtime 必須回報相同 lowercase sha256 image ID。
- pilot nginx `/router/*` hard deny、image inventory、air-gap、non-root 與 CI governance。
- workflow governance 使用 `yaml.safe_load()`，非法或不可解析 YAML fail-closed。

Closeout 修正 commits：

| Commit | 內容 |
|---|---|
| `913fbed` | contracts deep freeze 與 PKI posture acknowledgement |
| `66fb652` | 真 YAML parser 與 fail-closed governance |
| `c9e404f` | Gate 2 data boundaries |
| `d027960` | privileged session 與 artifact binding |
| `53eb87d` | runtime delegation、stream usage、單一 ALLOW closure |
| `e1f176d` | signed pilot v3 綁 CSP image content ID |
| `7154b18` | air-gap inventory 的 image-agent credential fixture |
| `d58f3a0` | auth race fixture 對齊 migration `r1_0019` |
| `ae8414c` | Router trace synthetic identity 避免 card-material scanner 誤判 |
| `736a021` | formal pilot lifecycle、9-service lock、overlay marker 與無 Studio nginx |

## 3. 驗證證據

### 3.1 Python、policy 與 deployment

```text
CSP focused                                    206 passed
anila-contracts + anila-security                76 passed
policy / CI                                     57 passed
deployment contracts                            98 passed, 1 Windows skip
Gate 2 policy                                   71 passed, 12 PG-only skipped
CSP startup + pilot runtime                     73 passed
anila-core trace / router                       68 passed
ingestion evaluator / judge                     58 passed
FLUX agent                                      64 passed
anila-core full                                806 passed, 10 skipped
ingestion-worker full                          188 passed
CSP full                                      1087 passed, 27 PG-only skipped
git diff --check                               PASS
```

27 個 CSP full-suite skips 都是 PG-only，已在下列 fresh PostgreSQL matrix 另跑。
第一次 full CSP 未提供 synthetic `SECRET_KEY`，因此有 12 個相同環境前置失敗；補上
測試用 key 後為上述 `1087 passed / 27 skipped`。這不是產品測試失敗。

整個 monorepo 的 broad Ruff 仍會看到 107 筆既有 lint debt；本輪 changed/focused scope
為綠。不得把 broad Ruff 誤報成 Gate 2 新 regression，也不得宣稱 repo-wide Ruff 全綠。

### 3.2 Fresh PostgreSQL

- Disposable image：`pgvector/pgvector:pg16`
- Alembic fresh upgrade：single head `r1_0019`
- Migration round-trip：`r1_0019 -> r1_0018 -> r1_0019` PASS
- Runtime 以 `csp_app` role；migration/fixture setup 才使用 admin role。

```text
core RLS integration                              9 passed
classification reconciliation                     3 passed
runtime classification invariants                 9 passed
SourceSnapshot / Citation sealing                10 passed
memory governance                                 2 passed
transaction / ledger / latch / atomicity          9 passed
latch races                                       2 passed
durable usage / Full Trace closure                2 passed
ingestion image RLS                               1 passed
auth races                                        3 passed
classification reconciliation checker             PASS
總計                                             50 passed + checker
```

### 3.3 Docker 與 pilot posture

Router／FLUX 由 `d58f3a0` 建置；CSP 因 startup posture 修正，已由 `736a021` 重建：

| Image | Content ID | Runtime posture |
|---|---|---|
| CSP | `sha256:eee0f7fcd0c7e380ce48312b6426eeda782e6ba3157f962c94c1f9558f042fce` | `USER=csp`, UID/GID `10001:10001` |
| Router | `sha256:f4075851ae56b28df2436499f4ab9dfb8ba1059d2c95a4075ff6f37155f295ae` | `USER=anila` |
| FLUX agent | `sha256:c8a1503c1933153e172df035525fcd0303c37c3c58f23dcf44c883a62d4125b1` | base image 仍為 root；pilot overlay 以 `gate3-artifacts` 排除 |

- CSP `--network none` 內部 package import：PASS。
- signed pilot 正確 CSP image ID + `gate2-pilot-v1` overlay marker：PASS。
- signed pilot 錯誤 CSP image ID：startup REJECTED；缺 overlay marker：startup REJECTED。
- actual Compose render：精確 9 services（CSP/DB、Redis、Router、兩個 UI、nginx、n8n、
  GitLab）；worker、Studio、pptx、FLUX、codeserver 均不在 active set。
- pilot nginx 在沒有 `anila-studio` DNS 時仍可 `nginx -t`／啟動；443/4443 的
  `/router/health` 與 Studio artifact API 都回 403；config/router/cert mounts 全為 ro。
- 測試 signed profile 僅保存 public trust，private keys 未持久化；smoke 後已刪除。

FLUX agent base image 的 root posture 不是目前 pilot exposure，因 overlay 明確排除該
capability；它仍是 Gate 3／production hardening 風險，不應遺忘。

### 3.4 正式 HTTP Full Trace 回讀

以目前 CSP image、fresh migrated PostgreSQL 與真實 Bearer login 路徑執行：

```text
login                                              PASS
POST /api/tasks                                    task_id=1
POST /v1/traces/{trace_id}/spans                   accepted=2, duplicates=0
GET  /api/traces/{trace_id}                        spans=2, task_id=1
span producer / classification                     proxy / 無機密
duplicate POST                                     accepted=0, duplicates=1
```

Trace ID：`ab22467574704cdb9e889fcc2dc22918`（只屬已刪除的 disposable DB）。測試 CSP
container、測試 database 與臨時簽章目錄都已清除；未保存 access token 或 DB URL。

## 4. GitHub review 狀態

Gemini 四個 review threads 已逐項回覆並 resolve：

1. `replace_document_chunks`：`_acquire()` 本身建立單一 transaction，failure test 與
   fresh PG atomicity 證明 rollback；不需 nested savepoint。
2. `add_parent_chunks`：同一 transaction boundary，第二筆 insert failure 整批 rollback。
3. ANILALM Task id：已要求 positive integer，避免 `NaN`。
4. governance YAML：已改 real parser 並補 malformed/multiline tests。

具名 reviewer 狀態：

- **Aegis — Approve**：246 focused tests；auth、clearance、artifact、signed pilot、
  zero-egress 無 blocker。
- **Sentinel — Approve**：64 focused/true-PG tests；交易、鎖序、RLS、latch、durable
  closure 與單一 ALLOW 無 blocker。
- **Parfit — Approve**：初審發現 formal deploy 未套 pilot overlay；`736a021` 修成單一
  authoritative lifecycle、精確 active set、CSP overlay-only marker、posture-aware
  verify 與無 Studio nginx。targeted re-review 以 62 deployment、41 policy、73 CSP
  tests 重新核對後通過。

Claude Opus 4.8 舊報告審的是 `d975f30`，兩個 High 已由 `53eb87d` 修正，不能當最終
簽核。exact `claude-fable-5` 將審本 handoff commit 後的 remote head；為避免把 verdict
寫回文件而再次改變 head，最終 model metadata/verdict 以 PR #27 closeout comment 為準。

## 5. Merge gate 與 Gate 3 邊界

PR #27 只有在以下全部成立時才能改成 ready/merge：

1. GitHub required CI 對最終 remote head 全綠。
2. 所有 review threads 維持 resolved，且沒有新 blocker。
3. Aegis、Sentinel、Parfit 各自明確 `Approve`。
4. exact `claude -p --model claude-fable-5` 對最終 head 明確 `Approve`，並由 session
   metadata 驗證實際 model。
5. worktree clean、remote head 等於本機 head，且 PR base 仍是 `main`。

Gate 3 roadmap 估計 7–11 週，不能把它當成 Gate 2 overnight closeout 的尾項。Gate 3
至少要延續：production artifact pipeline、FLUX non-root、完整 delivery matrix、DR restore
drill、正式卡片矩陣與 production deployment evidence。這些不應反向抹除 Gate 2 已通過的
pilot governance，但仍是 production blocker。

## 6. 公司端 resume commands

```powershell
git fetch origin --prune
git switch feat/gate2-governance-ledger
git pull --ff-only
git status --short --branch

gh pr checks 27 --repo zzw09773/ANILA
gh pr view 27 --repo zzw09773/ANILA --json headRefOid,isDraft,mergeable,reviews

$env:PYTHONIOENCODING='utf-8'
python C:\Users\USER\.codex\plugins\cache\openai-curated-remote\github\0.1.8-2841cf9749ae\skills\gh-address-comments\scripts\fetch_comments.py --repo zzw09773/ANILA --pr 27
```

安全提醒：不得提交 `.env`、DB URL、token、signed approval、private trust material、JWT/TLS
private key 或完整內網位置；runtime DB 只能使用 `csp_app`。
