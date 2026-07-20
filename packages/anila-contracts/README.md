# anila-contracts v2

ANILA 跨服務、跨 Agent framework 的最小 wire-contract 套件。

v2 的 top-level public API 包含 Gate 1 基礎、Gate 2 治理 envelope 與 Gate 5
控制面契約：

- `Classification`：五級分類與單向 max 規則。
- `StepEvent`：實際執行步驟的 `step-event/v1` envelope。
- `AgentError`：可安全向外傳遞的 `agent-error/v1` envelope。
- `TaskContext`：不含 caller-supplied clearance 的任務權威 context。
- `TraceContext`：task／run／invocation／session 的 trace correlation。
- `InvocationCommand`：具冪等、上限、registry revision 與交叉一致性驗證的派工命令。
- `SourceSnapshot`：含完整 document version map 與 SHA-256 的不可變來源快照。
- `SafeSummary`：有分類、redaction 記錄與 policy attribution 的安全投影。
- `RouteDecision`：Router 的結構化決策（取代 `DISPATCH:` 字串權威）。
- `PolicyGateResult`：Router/CSP policy gate 的 immutable 結果。
- `AgentManifest`：Agent 自宣告的能力、事件、分類與 model binding manifest；不含治理 authority。
- `ExecutionGrant`：綁定 task/run/trace/source/identity/target 且 TTL 不超過五分鐘的短效授權。

```python
from anila_contracts import (
    AgentError,
    AgentManifest,
    Classification,
    ExecutionGrant,
    InvocationCommand,
    PolicyGateResult,
    RouteDecision,
    SafeSummary,
    SourceSnapshot,
    StepEvent,
    TaskContext,
    TraceContext,
)
```

套件的唯一 runtime dependency 是 Pydantic；不依賴 FastAPI、asyncpg、pgvector、
sse-starlette 或任何 ANILA 服務套件。所有 v1/v2 model 都是 frozen、拒絕未知欄位與
未知 schema version，且 wire envelope 都必須明確攜帶版本；時間必須帶時區，
非 `none` 的來源快照必須有小寫 SHA-256 與完整 document version map；`origin=none`
則不得夾帶 content hash 或其他來源證據。正式 clearance 仍由 CSP policy 資料推導，
不是可由 caller 放進 `TaskContext` 的 wire 欄位。

`AgentManifest` 只描述 Agent 自身宣告；approval、health、readiness、trace-test
證據、manifest revision 與 `ready_for_dispatch` 都由 CSP 計算並放在 registry
snapshot，不能由 Agent 自報。`PolicyGateResult` 與 `ExecutionGrant` 會攜帶
route/policy/registry identity，供後續 gate 逐段比對。

為補強 frozen 語意，序列欄位在 Python model 內使用 tuple（JSON 仍輸出 array）。Pydantic
的 `frozen=True` 不會把 JSON object 深凍結；`InvocationCommand.input` 與
`SourceSnapshot.document_versions` 仍是 dict。呼叫端不得把 model instance 當防竄改
邊界，跨邊界前後都必須重新驗證／序列化。

CSP 自己的相容 facade 仍以 `ClassificationLevel` 提供舊 import 名稱，但它與
`Classification` 是同一個 class，不建立第二套分類語意。事件 kind/status、錯誤碼、
輔助 enum 與 schema version constant 是契約內部 schema 元件，不屬於 top-level public
API。
