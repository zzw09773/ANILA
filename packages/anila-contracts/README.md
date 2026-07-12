# anila-contracts v0

ANILA 跨服務、跨 Agent framework 的最小 wire-contract 套件。

v0 的 top-level public API 刻意只包含三個契約：

- `Classification`：五級分類與單向 max 規則。
- `StepEvent`：實際執行步驟的 `step-event/v1` envelope。
- `AgentError`：可安全向外傳遞的 `agent-error/v1` envelope。

```python
from anila_contracts import AgentError, Classification, StepEvent
```

套件的唯一 runtime dependency 是 Pydantic；不依賴 FastAPI、asyncpg、pgvector、
sse-starlette 或任何 ANILA 服務套件。TaskContext、Invocation、Manifest、Policy 等
後續契約不屬於 Gate 1 F5 / v0 範圍。

CSP 自己的相容 facade 仍以 `ClassificationLevel` 提供舊 import 名稱，但它與
`Classification` 是同一個 class，不建立第二套分類語意。事件 kind/status、錯誤碼與
schema version constant 是三個契約的內部 schema 元件，不屬於 top-level public API。
