# Gate 5 R6 frozen routing eval

`routing-eval.v1.json` 是 Router runtime 接入前的 frozen fixture。這一批只提供
可審計的資料集與 static verifier；不呼叫模型、不執行 Router，也不從
`expected` 欄位反推 100% 的假 metric。真正的 runtime runner 由後續 R3/R6
工作接上。

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

這些 threshold 目前只作 frozen policy metadata 與 static drift check；本批不
宣稱已取得 runtime quality 結果。

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
