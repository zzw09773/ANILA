# Gate 6 P9 enabled inference callsite evidence

`generate_p9_enabled_callsite_inventory.py` 是 Gate 6 P9 的唯讀 evidence
exporter。它不修改 inventory、profile、trust store 或 source tree，也不執行
任何模型／網路呼叫；輸出只是供 P9 後續 network capture、egress deny 與 usage
reconciliation 使用的 machine-readable evidence。

## 驗證邊界

CLI 會先呼叫 Gate 5 `check_model_governance.verify()`，再由同一個
`VerifiedModelGovernanceAuthority` 建立輸出；enabled/production 模式另外必須
提供並驗證 Gate 6 P0 signed acceptance profile。P0 的
`enabled_inference_callsite_inventory` 會逐項與 Gate 5 inventory／authority
cross-check：`schema_version`、`version`、`sha256` 與完整 `callsite_ids` 任何一項
不符都 fail closed。因此以下任一項失敗都會 non-zero exit 且不產生 evidence：

- source scanner 與 static inventory 不一致、source 遺失或 inventory digest 不符；
- profile 欄位／profile content hash 不符、enabled ID 未完整 partition inventory，
  或 binding 指向未知 callsite、artifact、deployment；
- production profile 缺 out-of-band trust store、四方 Ed25519 signature、授權模型
  artifact、fresh/healthy deployment 或唯一 CSP gateway；
- enabled profile 缺 `--acceptance-profile`／`--acceptance-trust-store`，P0 profile
  缺五方簽章、content hash、效期／觀測窗，或 P0 inventory 與 Gate 5 不一致；
- raw endpoint、usage/audit sink、classification ceiling、agent scope 漂移；
- disabled template（除非明確使用下方測試旗標）。

Exporter 不重新實作簽章或 admission crypto；Gate 5 model-governance authority
負責 source inventory、artifact、deployment 與四方 profile，Gate 6 P0 verifier
負責五方 acceptance profile；exporter 只做兩份已驗證 inventory projection 的
cross-check 與 canonical projection。

## CLI

從 repository root 執行（`anila-security` 可由本機 source tree 自動載入）：

```powershell
python infra/policy/gate6/generate_p9_enabled_callsite_inventory.py `
  --repo-root . `
  --inventory infra/policy/gate5/model-governance-inventory.v1.json `
  --profile C:\outside\governance\signed-profile.json `
  --trust-store C:\outside\governance\trust-store.json `
  --acceptance-profile C:\outside\governance\production-acceptance-profile.json `
  --acceptance-trust-store C:\outside\governance\production-acceptance-trust-store.json `
  --generated-at 2026-07-15T12:00:00Z `
  --output C:\outside\evidence\p9-enabled-callsites.json
```

`--generated-at` 應由 evidence 呼叫者提供，才能讓相同輸入得到相同
`content_sha256`。未提供時 exporter 使用目前 UTC 時間，仍會輸出有效 evidence，但
每次輸出的 hash 會不同。

enabled/production mode 不允許省略 P0 兩個參數；這是 P9「從 P0 signed profile
自動產生」的硬性邊界，不可用 Gate 5 model-governance profile 取代。

Repository 附的 disabled profile 只能在測試／盤點時使用：

```powershell
python infra/policy/gate6/generate_p9_enabled_callsite_inventory.py `
  --repo-root . `
  --inventory infra/policy/gate5/model-governance-inventory.v1.json `
  --profile infra/policy/gate5/model-governance-profile.disabled-template.json `
  --test-only-allow-disabled-template `
  --generated-at 2026-07-15T12:00:00Z
```

這個模式輸出的 `status`／`acceptance_status` 是 `NOT_ACCEPTANCE`、
`environment` 是 `non-production`、`gate6_pass` 永遠是 `false`，且
`enabled_callsites` 必須是空陣列；此模式可不提供 P0 profile。未帶測試旗標時
disabled template 一律拒絕。

## Output contract

輸出採 Gate 6 P9 schema
`anila.gate6.p9.enabled-inference-callsite-evidence.v1`，以 shared
`canonical_json`（UTF-8、`ensure_ascii=false`、sorted keys、compact separators）
序列化。根層至少包含：

- `source_inventory_sha256` 與 inventory id/version；
- signed profile id/version、`signed_profile_content_sha256`；
- P0 acceptance profile id/version/content hash、五方 `signer_roles`，以及其綁定的
  enabled callsite inventory；
- `artifact_digests`（model artifact digest）與 `deployment_digests`（image digest）；
- 依 callsite ID stable sort 的 `enabled_callsites`。每筆含 category/kind、
  component/service、owner、path/source、symbol、gateway、`raw_endpoint`、分類
  ceiling、usage/audit sink、agent scope，以及所綁 artifact/deployment digest；
- `content_sha256`，其值是 canonical root object 移除自身欄位後的 SHA-256。

輸出不是 Gate 6 Go 宣告：`gate6_pass` 固定為 `false`，即使 Gate 5 與 P0 signed
profiles 都驗證成功，也只代表 P9 inventory evidence 的輸入契約已驗證；network
capture、egress deny 與 usage reconciliation 仍未完成。

## 尚未由此 CLI 證明的 P9 條件

P9 的退出條件仍需要 production-equivalent 實證，這個 exporter 不會冒充完成：

1. 每個 enabled callsite 的 runtime wiring／admission model binding 要逐項對應；
2. host/container/network 層的 deny、packet capture、裸 `ANILA_BASE_URL`／裸模型
   網段／裸 T2I URL negative tests 尚未由本工具執行；
3. 每次推論的 CSP usage 與 audit row 尚未做 live reconciliation；
4. Router、正式 Agent、embedding、Studio／FLUX、Memory、prompt generator、
   Ingestion relation／Judge 等 callsite 的 production owner、capture artifact 與
   五方 Gate 6 acceptance sign-off 仍需另行提供。
