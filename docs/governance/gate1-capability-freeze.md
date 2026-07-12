# Gate 1 F7 能力凍結政策

Gate 1 到 Gate 5 完成前，ANILA 不接受未經架構例外核准的新 agent 類型、artifact
類型、memory 功能或自由 multi-agent 能力。這不是功能旗標；它是 required CI
policy check 的來源資料與執行契約。

## 執行

在 repo root 執行：

```powershell
py -3.11 infra/policy/gate1/check_capability_freeze.py
py -3.11 -m unittest infra.policy.tests.test_gate1_capability_freeze
```

PR required check 必須使用受信任的 base ref，且 checkout 要有完整 base history：

```powershell
py -3.11 infra/policy/gate1/check_capability_freeze.py `
  --base-ref "origin/$env:GITHUB_BASE_REF" `
  --bootstrap-if-missing
```

`--bootstrap-if-missing` 只在首次導入、base branch 尚無 F7 baseline 時生效；baseline
一旦進入 base，後續 PR 即使同時改程式與 baseline 也不會把新增能力洗白。工具會以
base branch 的受信任版本比對，並拒絕工作分支竄改 baseline。CI 若沒 fetch 到 base
ref 也會 fail-closed，而不是退回使用不受信任的 working-tree baseline。

第一個命令會從實際程式碼重新盤點，與
`infra/policy/gate1/capability-freeze-baseline.json` 比對。任何新增項目若沒有有效
例外，命令會以 exit code 1 結束；baseline、例外或受控原始碼無法解析時會以 exit
code 2 fail-closed。移除既有能力不屬 F7 禁止事項，因此只列為 non-blocking
baseline removal，仍應在 PR 中說明並更新後續盤點。

## 目前鎖定的實際表面

- Agent：CSP `RuntimeType` 五值，以及正式／dev compose 的
  `AUTO_REGISTER_AGENTS` 宣告 ID（目前只有 `image-generator`）。
- Artifact：CSP `ArtifactType` 五值、Studio 五個 job service 與五組正式 HTTP
  job/download route。
- Memory：anila-core 的 type/scope、anila-agent 的四型 taxonomy、兩個套件的
  module、public symbol、public class method 與 `__all__` export、CSP memory
  service public symbol/method 與 `/api/memory/*` route。
- Multi-agent：`Coordinator` public methods、agent-as-tool／dispatch tool
  entrypoint、Router dispatch/handoff entrypoint、具名 multi-agent source files，以及
 現有能力被其他 production source 暴露的位置。

盤點不是靠 README 字串；Python enum、class、function、FastAPI decorator、module
路徑與 compose 宣告均直接從 source 收集。目前 baseline 有 25 個 surface、369 個
entry。

## 例外契約

例外只寫入 `infra/policy/gate1/capability-freeze-exceptions.json`，每筆必須精確列出
`surface::value`，不能用 wildcard。必要欄位如下：

```json
{
  "id": "F7-EXAMPLE-1",
  "architecture_owner": "Lin Mei-Hua",
  "rationale": "書面說明為何 Gate 5 前必須先加入，以及不加入的實際影響。",
  "ticket": "ANILA-1234",
  "approved_on": "2026-07-12",
  "expires_on": "2026-08-12",
  "changes": [
    "artifact.types::audio"
  ]
}
```

`architecture_owner` 必須是具名的人，`rationale` 必須具體，`ticket` 必須是可追蹤
ID 或 URL，且例外必須已核准、未過期。過期、重複授權、placeholder owner、空泛理由
或未精確列出的第二個新增項目都會失敗。單人維護時，具名 owner 可以是 repo owner
本人，但仍須透過 PR 留下書面核准紀錄。

例外不會改寫 baseline；因此它到期後，仍存在的新增能力會重新變成 required check
紅燈。Gate 5 結束後應由架構決策明確解除或改版這份 freeze，不應靜默刪除 check。

## 靜態檢查界線

這個 check 能可靠阻擋受控 registry、型別、route、module、public entrypoint 與既有
multi-agent 曝露面的擴張，但無法判斷刻意使用無關名稱、藏在既有 private helper
內的語意新功能。因此 PR review 仍須把任何 agent dispatch、artifact generator、
memory persistence/recall 或 worker orchestration 的新增行為視為 F7 範圍；靜態 check
是 required guard，不是架構審查的替代品。
