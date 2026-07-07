# ANILA 大腦技術規格草案 — `RouteDecision` / `PolicyGate` / `StreamBridge`

> 日期：2026-07-06  
> 目的：提供可直接進入設計審查或派工的技術規格草案。

---

# Part A：`RouteDecision` 規格

## A1. 設計目標

`RouteDecision` 是 Router 對一次使用者請求的結構化判斷結果。它取代自由文字或 `DISPATCH:<agent_id>:<query>` 類字串控制契約。

## A2. 不變式

```text
任何 agent dispatch 必須由有效 RouteDecision 產生
未知 agent_id 不可 dispatch
invalid schema 不可 dispatch
低 confidence 時應 fallback 或 clarify
reason_codes 必須可 audit
```

## A3. Schema 草案

```python
from typing import Literal
from pydantic import BaseModel, Field

class PolicyRequirements(BaseModel):
    classification_ceiling: str | None = None
    requires_audit: bool = True
    requires_approval: bool = False
    required_scopes: list[str] = Field(default_factory=list)

class FallbackDecision(BaseModel):
    route_type: Literal["direct_answer", "clarify", "deny"]
    message: str | None = None
    reason_code: str | None = None

class AgentStep(BaseModel):
    step_id: str
    agent_id: str
    input: dict
    depends_on: list[str] = Field(default_factory=list)
    timeout_ms: int | None = None

class ExecutionPlan(BaseModel):
    type: Literal["single_agent", "multi_agent_plan"]
    steps: list[AgentStep]
    merge_strategy: str | None = None

class RouteDecision(BaseModel):
    route_type: Literal[
        "direct_answer",
        "single_agent",
        "multi_agent_plan",
        "clarify",
        "deny"
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list)
    user_visible_reason: str | None = None

    direct_answer: str | None = None

    agent_id: str | None = None
    rewritten_query: str | None = None

    plan: ExecutionPlan | None = None
    fallback: FallbackDecision | None = None
    policy_requirements: PolicyRequirements = Field(default_factory=PolicyRequirements)
```

## A4. 驗證規則

| route_type | 必要欄位 | 禁止或限制 |
|---|---|---|
| `direct_answer` | `direct_answer` 或後續 provider call | 不可有 `agent_id` |
| `single_agent` | `agent_id`, `rewritten_query` | `agent_id` 必須在 candidate list |
| `multi_agent_plan` | `plan.steps` | 每個 step agent 必須通過 policy |
| `clarify` | `user_visible_reason` | 不可 dispatch |
| `deny` | `user_visible_reason`, reason code | 不可 dispatch |

## A5. Reason code 建議

```text
USER_REQUESTED_IMAGE
USER_REQUESTED_DOCUMENT_GENERATION
USER_REQUESTED_KNOWLEDGE_SEARCH
USER_REQUESTED_AGENT_BY_NAME
INSUFFICIENT_INFORMATION
NO_AGENT_MATCH
LOW_CONFIDENCE
POLICY_RESTRICTED
CLASSIFICATION_TOO_HIGH
AGENT_UNAVAILABLE
PROMPT_INJECTION_SUSPECTED
```

## A6. Invalid decision fallback

```text
invalid JSON
  ↓
trace invalid_decision
  ↓
不 dispatch
  ↓
若 safe，可 direct answer：「我需要更多資訊才能選擇工具」
  ↓
否則回 generic error
```

---

# Part B：`PolicyGate` 規格

## B1. 設計目標

`PolicyGate` 是 Router dispatch 前的強制治理檢查。它不取代 CSP，但防止 Router 自己成為繞過治理的捷徑。

## B2. Interface 草案

```python
class PolicyGateInput(BaseModel):
    request_id: str
    trace_id: str
    user_id: str
    user_scopes: list[str]
    task_id: str | None
    classification: str | None
    route_decision: RouteDecision
    agent_manifest: dict | None

class PolicyGateResult(BaseModel):
    action: Literal["allow", "deny", "clarify", "approval_required"]
    allowed: bool
    reason_code: str
    safe_message: str | None = None
    audit_required: bool = True
```

## B3. 檢查順序

```text
1. RouteDecision schema 已驗證
2. route_type 是否需要 dispatch
3. agent_id 是否存在於 registry snapshot
4. agent status 是否 approved / ready
5. user scope 是否滿足 required_scopes
6. request classification 是否 <= agent classification_ceiling
7. endpoint 是否只能經 CSP proxy 或 trusted path
8. SSRF / trusted hosts 是否通過
9. 高風險操作是否需要 approval
10. 回傳 allow / deny / clarify / approval_required
```

## B4. Denial reason code

```text
UNKNOWN_AGENT
AGENT_NOT_APPROVED
AGENT_UNHEALTHY
MISSING_SCOPE
CLASSIFICATION_EXCEEDS_AGENT_CEILING
UNTRUSTED_ENDPOINT
APPROVAL_REQUIRED
POLICY_CONFIG_MISSING
```

## B5. Fail-closed 規則

以下狀況預設 deny：

```text
agent manifest 缺分類上限且 task 有分類
agent endpoint 無法驗證
user scope 無法取得
agent status unknown
policy config 無法讀取
CSP registry snapshot 過期且無安全 cache
```

---

# Part C：`StreamBridge` 規格

## C1. 設計目標

`StreamBridge` 將 agent / provider 的 streaming 輸入轉成 Router 內部標準事件，再輸出為 OpenAI-compatible SSE 與 ANILA richer events。

## C2. 標準事件模型

```python
class StreamEvent(BaseModel):
    event_id: str
    cursor: int | None = None
    event_type: str
    payload: dict
    trace_id: str
    session_id: str | None = None
    created_at: str
```

## C3. Event type

```text
router.decision
agent.start
agent.delta
agent.tool_call
agent.artifact
agent.warning
agent.error
agent.done
router.fallback
session.checkpoint
```

對外 SSE event 可映射成：

```text
anila.router.decision
anila.agent.start
anila.agent.delta
anila.agent.tool_call
anila.agent.artifact
anila.agent.error
anila.agent.done
anila.router.fallback
```

## C4. SSE parser 要求

必須支援：

```text
event: name
data: line1
data: line2

: heartbeat

data: [DONE]
```

多行 `data:` 合併規則：

```text
同一 SSE event 內的多個 data line 必須以 newline 合併
不可只取最後一行
不可把不同 event 合併
```

## C5. Error 行為

| agent 行為 | Router 行為 |
|---|---|
| agent 回 OpenAI error chunk | 轉成 normalized error event |
| agent 中途斷線 | 發 `anila.agent.error`，再依 fallback policy 決定是否 `[DONE]` |
| agent 回非 JSON | 若是 plain text delta，可包成 delta；否則 invalid response |
| agent 無 `[DONE]` | timeout 後產生 stream broken |
| client disconnect | 停止往 client 寫，取消 downstream 或進入 detached mode |

## C6. Resume hook

StreamBridge 每產生一個標準事件，應可呼叫：

```python
session_store.append(session_id, stream_event)
```

Resume 時：

```python
events = session_store.read_after(session_id, cursor)
```

---

# Part D：測試案例草案

## D1. RouteDecision tests

```text
test_valid_single_agent_decision
test_unknown_agent_rejected
test_invalid_json_rejected
test_prompt_injection_dispatch_text_ignored
test_low_confidence_clarify
test_multi_agent_plan_requires_all_steps_valid
```

## D2. PolicyGate tests

```text
test_policy_allows_approved_agent
test_policy_denies_unapproved_agent
test_policy_denies_missing_scope
test_policy_denies_classification_ceiling
test_policy_denies_untrusted_endpoint
test_policy_fail_closed_when_manifest_missing
```

## D3. StreamBridge tests

```text
test_sse_multiline_data_preserved
test_named_event_converted_to_anila_event
test_done_emitted_once
test_agent_error_normalized
test_broken_stream_emits_error
test_client_cancel_stops_downstream
```

## D4. Integration tests

```text
test_router_direct_answer_no_agent_called
test_router_single_agent_dispatch_via_fake_csp_proxy
test_router_policy_denied_does_not_call_agent
test_router_agent_stream_to_openai_sse
test_router_resume_from_cursor
```

---

# Part E：MVP 驗收

MVP 完成後，應可證明：

```text
1. Router 不再依賴自由文字作為 dispatch 權威
2. 任何 dispatch 前都經過 PolicyGate
3. agent stream 可以被標準化
4. route decision 可以被 trace/audit
5. prompt injection 不會觸發 agent dispatch
6. fake CSP / fake model / fake agent 可測完整流程
```
