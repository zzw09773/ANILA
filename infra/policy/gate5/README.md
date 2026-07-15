# Gate 5 R6 frozen routing eval

`routing-eval.v1.json` 是不可依結果調整的 frozen fixture。Static verifier 仍只
驗證資料集 identity、denominator 與 threshold，不呼叫模型；Gate 5 現已另以
`infra.ci.gate5_r3_eval_adapter:build_adapter` 接上正式 R3 `ExecutionRuntime`，由
required routing runtime exit job 實際執行全部 160 筆 case。Adapter 只能讀取
immutable `input`／`messages`／`context` projection，不能讀取 `expected`、case id
或 category，因此 runtime metric 不是從答案欄反推的假分數。

## Frozen identity

- `schema_version`: `anila.routing-eval.v1`
- `dataset_id`: `gate5-routing-eval`
- `dataset_version`: `1.0.0`
- `case_count`: **160**
- `dataset_sha256`: `083eaa2725553d260ebd45035d804c34679af322b67cadcf14ac90490b0d9707`
- verifier：`python infra/ci/check_gate5_routing_eval.py`

Hash 是 canonical UTF-8 JSON 的 SHA-256。計算時移除根層
`dataset_sha256` 自身，再以 `sort_keys=true`、`ensure_ascii=false`、compact
separators `(',', ':')` 序列化；因此 JSON 排版不是 hash 契約的一部分，但
資料內容、陣列順序與 denominator membership 都是 frozen 的。

## Case distribution

| category | count |
| --- | ---: |
| `direct_answer` | 20 |
| `single_agent` | 60 |
| `clarify` | 15 |
| `prompt_injection` | 15 |
| `permission_denied` | 10 |
| `classification_denied` | 10 |
| `agent_unavailable` | 10 |
| `multi_intent` | 10 |
| `streaming_abnormal` | 5 |
| `resume` | 5 |
| **sum** | **160** |

`single_agent` 維持三個正式 Agent 各 20 筆；至少 15 筆／Agent 使用不同的
領域任務與措辭，最多 25% 的 input 直接出現 Agent ID 或 canonical capability
token。資料包含英文與繁體中文、政策、維運、採購、稽核、工安、網路與值勤等
不同語意；不是僅替換 case number 的模板。

`streaming_abnormal` 與 `resume` 是低權重但不可省略的安全回歸類別；各保留
5 筆，確保 streaming/resume fallback 有固定覆蓋。verifier 與 tests 會同時檢查
`sum(category_counts) == case_count == 160`、三 Agent 分布、normalized input
unique ratio（至少 0.8）與 context diversity（至少 40 個 canonical contexts）。

## Denominator membership

Denominator 不用「看到結果後再篩選」，而是在資料集內以完整 `case_ids` 明列：

| denominator | membership | count |
| --- | --- | ---: |
| `route_top1` | `single_agent` | 60 |
| `false_dispatch` | `direct_answer`、`clarify`、`prompt_injection`、`permission_denied`、`classification_denied`、`agent_unavailable`、`multi_intent` | 90 |
| `policy_bypass` | `permission_denied`、`classification_denied` | 20 |

`policy_bypass` 是 `false_dispatch` 的子集合，故 denominator 彼此不要求互斥。
Verifier 會檢查每個 ID 存在、無重複、`count == len(case_ids)`，以及 membership
的完整 case ID set 與實際 category。這批資料的 quality threshold 是：

- `route_top1_min`: `0.95`
- `false_dispatch_max_exclusive`: `0.01`
- `policy_bypass_max`: `0`

這些 threshold 同時是 frozen policy metadata、static drift check 與 required
runtime exit gate。正式 R3 adapter 的實跑結果為：`route_top1=60/60=1.0`、
`false_dispatch=0/90`、`policy_bypass=0/20`，全部通過 frozen threshold。

## Case contract

每筆 case 嚴格只接受以下欄位：

- `id`, `category`, `input`, `messages`, `context`, `expected`,
  `must_not_route_to`
- `messages` 僅允許 `role`/`content`；每筆固定一個 user message，且與 `input`
  完全相同。
- `context` 是 trusted context，固定包含 `classification`、`scopes`、
  `available_agent_ids`、`capabilities`、`health`、`registry_snapshot_id`、
  `requested_agent_id`、`agent_profiles`。health 記錄 Router 與三個 Agent 的
  `healthy`/`unhealthy`/`unknown` 狀態；profiles 固定提供每個 Agent 的
  classification ceiling 與能力集合。
- `expected` 固定包含 `route_type`、`selected_agent_id`、`policy_allowed`、
  `policy_reason_codes`、`fallback`。非 `single_agent` 不得帶 selected Agent；
  `must_not_route_to` 必須覆蓋所有不應被派工的 available Agent。

Prompt-injection cases 覆蓋至少五個 family：英文 ignore/disregard、繁中忽略、
role/system token、legacy `DISPATCH:` 誘導、資料外洩／工具 override。Verifier
依 family pattern 驗證，不硬鎖單一英文片語。`agent_unavailable` 則分散覆蓋三個
Agent 以及 `unhealthy`／`unknown`，不把 unavailable 寫死成 image service。

所有巢狀 object 皆採 exact-field、unknown-field fail-closed；identifier、
classification、health、scope/reason token 也會驗證。fixture 不含 API key、
JWT、service token 或其他 credential-like 字串。

## Change discipline

這是 frozen test set。只有在明確的 dataset 版本變更 PR 中，才能同步修改
cases/category counts/denominator membership，並重新計算 `dataset_sha256`、
更新 `dataset_version` 與本 README。不得先執行 runtime 結果，再為了改善
分數刪除、替換或重分類 case。新 runtime runner 必須輸出 dataset version/hash、
每個 denominator 的 numerator/denominator 與 runner commit，並維持本檔的
membership 與 thresholds。

## R7 model governance and deployment boundary

`model-governance-inventory.v1.json` 是目前 source-level inference sink 的
可審計清單；Gate 5 worktree 會把 Router、formal Agent、embedding、Studio／FLUX、
Memory、prompt generator、ingestion relation／judge 與模型 backend sink 綁到
唯一 `csp-model-gateway`、classification ceiling、usage／audit sink、agent scope
以及 source scanner。新增而未登錄的 inference sink 會讓 verifier fail-closed。

其中 `r3.csp.agent-dispatch` 是獨立於 Router→CSP client 的 CSP→Agent trust
boundary：它治理 signed ExecutionGrant envelope、per-agent outbound dispatch
與 durable `csp.session_events` receipts；Agent 內部實際模型呼叫仍須另由 R7
model binding、usage 與 audit sink 治理，這個 entry 不得被解讀為模型授權本身。

`model-governance-profile.disabled-template.json` 明確是「未啟用、不是 production
approval」的 repository template。它沒有 approver、signature、model artifact 或
deployment；只有帶 `--allow-disabled-template` 的 static verifier 才可接受。任何
production enabled profile 仍必須另具備 signed profile、artifact／deployment
digest、license／法務裁決、GPU topology、fresh health/readiness、四類 approver 與
唯一 CSP gateway binding。

本批 R7 建立 static foundation（contracts、source inventory、hash／signature
verifier 與 negative tests），並接上目前已完成範圍的 CSP runtime admission 與
deployment integration；這不宣稱所有 inference callsite 都已完成 runtime
接線。正式 profile 啟動時，CSP 會
從四個 repo 外的唯讀掛載載入 inventory、signed profile、trust store 與 observed
deployment facts，驗證 signature／digest／license／deployment readiness 後才讓
model call 通過；缺檔、過期、撤銷、authority rotation 或 pre/post usage／audit
receipt 失敗都 fail-closed。治理 runtime 不直接執行模型網路 I/O，實際部署邊界
由 resolved Compose 的 deployment checker 與 network topology 雙重拒絕：formal
profile 只有 CSP 可加入 `anila-models-net`，Agent 不得拿任意 raw model URL，且
governance material 必須是 CSP 的 read-only mounts。`deploy-prod.sh preflight`
會對 platform 與獨立 model stack 的 `docker compose config --format json` 執行
`check_deployment_egress.py`；development profile 只保留明示的 testable direct
model egress，不得被當成 production approval。這是 deployment-time topology
guard，不取代主機／容器層的 packet-level egress firewall 或外部 usage
reconciliation 報表。

Repository 只附 disabled template 與 synthetic-material generator（測試時在
記憶體產生 ephemeral signing key，輸出不含 private key），不附任何 production
approval。Generator 預設只啟用目前可實際執行且 agent scope 為空的
`r7.csp.memory`；其餘 37 個 inventory callsite 維持 disabled。`r7.csp.proxy`
與 `r7.csp.proxy-service` 雖有來源碼接線，但目前 signed binding 宣告
`registered-agent` scope，而 CSP proxy admission 沒有傳入 caller-agent context；
因此缺少該 context 時必須在 network 前 fail-closed，不能把它們冒充成 live
synthetic smoke。可用重複的 `--callsite-id` 做明確的非-FLUX CSP fixture（例如
authority/schema 測試），但 raw／FLUX／unknown callsite 會在簽署前拒絕。所有
輸出都標示 synthetic test-only，不能冒充 operator production approval。FLUX.2-dev
的 BFL Non-Commercial 限制尚未取得正式法務簽核；因此
formal platform 預設不註冊 image model／Agent，獨立 model stack 的 `flux2-dev`
與 model-side shim 僅在明示 `flux-approved` profile、`GATE5_FLUX_LEGAL_APPROVED`
與 signed profile 的 FLUX callsite binding 同時成立時才可啟動。disabled template
不能被部署工具或 runtime 當成可用模型授權，也不代表 routing／model quality 已
有 live metric。

Static check：

```bash
PYTHONPATH=packages/anila-security/src python \
  infra/policy/gate5/check_model_governance.py \
  --repo-root . \
  --inventory infra/policy/gate5/model-governance-inventory.v1.json \
  --profile infra/policy/gate5/model-governance-profile.disabled-template.json \
  --allow-disabled-template
```

正式部署的 `GATE5_MATERIAL_DIR` 是 repo 外的四個唯讀檔案；CLI 的
`--repo-root` 必須明確指向實際 source tree，不能由外部 inventory 路徑推導。
模型 lifecycle 會以 `docker network create --driver bridge --internal
anila-models-net` 建立並在每次 preflight／up／restart／status 讀回
`Internal=true`；既有錯誤 network 只會 fail-closed 並印出人工確認 attached
containers 後的安全重建指令。

可在有 Docker daemon 與本地 `python:3.11-slim` image 時執行 live topology
negative smoke（不開 host port、以 direct IP/TCP 驗證）：

```bash
bash infra/deployment/scripts/gate5-network-negative-smoke.sh
```

R6 的 deterministic contract runner 只接受明確的 R3 runtime adapter interface；
adapter 只會看到 immutable 的 `input`／`messages`／`context` projection，不能讀到
case id、category、`expected` 或 `must_not_route_to`。未提供 adapter 時 runner 仍會
以 non-zero incomplete code 回報 `SKIPPED`，不會拿 frozen `expected` 欄位冒充
prediction，也不會產生假 metric；但 Gate 5 required routing runtime exit job
現在已明確傳入正式 R3 adapter，不再處於 `SKIPPED`／incomplete 狀態。

Required job 會先執行 adapter 與 mutation-guard tests，再以 runner 實跑 frozen
dataset；目前 read-back 為 `route_top1=60/60=1.0`、`false_dispatch=0/90`、
`policy_bypass=0/20`。報告包含 dataset version/hash、runner commit 與各 denominator
的 numerator／denominator。這只證明 Gate 5 frozen routing contract，不能解讀為
production signed model profile、實體環境容量或 Gate 6 法務／人員簽核；R7 的
production profile 仍維持 disabled，FLUX 法務界線亦未改變。
