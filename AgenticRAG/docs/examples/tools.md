# 自訂 Tool

AgenticRAG agent 用 tool-calling 跟外界互動。內建工具：
- `vector_search` / `keyword_search` / `read_document` — RAG 三件套
- `web_search` (optional) — 外部搜尋
- 還有 framework runtime 自己的 file / shell / patch tools

要加你業務的 tool（查 CRM、發 email、call 公司內部 API 等）這份文件示範。

## Tool 是什麼

每個 tool 是一個 `ToolDefinition`：
- **name**：LLM 用來呼叫的名字（snake_case）
- **description**：LLM 看的說明，越清楚 model 越會在對的時機呼叫
- **parameters**：JSON Schema 定義 tool 接受的參數
- **handler**：async callable，接 dict 參數，回任何 JSON-serializable 結果

## 範例 1：簡單 tool（查內部 CRM）

```python
# my_app/tools/crm.py
from agentic_rag.tools.types import ToolDefinition


async def lookup_customer(args: dict) -> dict:
    """Tool handler — 接 LLM 給的參數，回查詢結果。"""
    customer_id = args.get("customer_id")
    if not customer_id:
        return {"error": "customer_id required"}

    # 你的 CRM 查詢邏輯
    customer = await crm_db.get(customer_id)
    if customer is None:
        return {"error": f"customer {customer_id} not found"}

    return {
        "id": customer.id,
        "name": customer.name,
        "tier": customer.tier,
        "active_orders": customer.active_orders_count,
    }


CRM_TOOL = ToolDefinition(
    name="lookup_customer",
    description=(
        "Look up a customer's profile by ID. Returns name, tier "
        "(bronze/silver/gold), and number of active orders. "
        "Use when the user asks about a specific customer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "customer_id": {
                "type": "string",
                "description": "Customer ID, e.g. 'C-12345'",
            },
        },
        "required": ["customer_id"],
    },
    handler=lookup_customer,
)
```

## 註冊到 ToolRegistry

```python
# my_app/server.py
from agentic_rag.api.server import create_app
from agentic_rag.router.tool_router import ToolRegistry

from my_app.tools.crm import CRM_TOOL
from my_app.tools.email import SEND_EMAIL_TOOL  # 另一個 tool

registry = ToolRegistry()
registry.register(CRM_TOOL)
registry.register(SEND_EMAIL_TOOL)

# 內建 RAG 工具也要 register
from agentic_rag.tools.rag_tools import VECTOR_SEARCH_TOOL, KEYWORD_SEARCH_TOOL
registry.register(VECTOR_SEARCH_TOOL)
registry.register(KEYWORD_SEARCH_TOOL)

app = create_app(provider=..., tool_registry=registry, ...)
```

## 範例 2：有副作用的 tool（送 email）

對於會「真的執行動作」的 tool（送 email、改資料庫、call 外部 API），加 idempotency 與 audit。

```python
# my_app/tools/email.py
import logging
import uuid
from agentic_rag.tools.types import ToolDefinition

logger = logging.getLogger(__name__)


async def send_email(args: dict) -> dict:
    to = args.get("to")
    subject = args.get("subject")
    body = args.get("body")
    idempotency_key = args.get("idempotency_key") or str(uuid.uuid4())

    if not all([to, subject, body]):
        return {"error": "to/subject/body all required"}

    # 檢查 idempotency — 同 key 不重送
    if await idempotency_store.exists(idempotency_key):
        return {"status": "already_sent", "idempotency_key": idempotency_key}

    # 真的送
    try:
        await mail_client.send(to=to, subject=subject, body=body)
    except Exception as exc:
        logger.exception("email send failed")
        return {"status": "failed", "error": str(exc)}

    await idempotency_store.set(idempotency_key)
    logger.info("email sent to=%s subject=%s key=%s", to, subject, idempotency_key)
    return {"status": "sent", "idempotency_key": idempotency_key}


SEND_EMAIL_TOOL = ToolDefinition(
    name="send_email",
    description=(
        "Send an email. CONFIRM with the user BEFORE calling — this "
        "tool has real-world side effects. Provide a unique "
        "idempotency_key on retry to avoid duplicate sends."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "format": "email"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "idempotency_key": {
                "type": "string",
                "description": "UUID. Same key = same operation.",
            },
        },
        "required": ["to", "subject", "body"],
    },
    handler=send_email,
)
```

實務 tip：在 description 寫「**CONFIRM with the user BEFORE calling**」效果不錯 — 大模型通常會先問。要更可靠請考慮 anila-core 的 `tool_approval` interrupt 機制（讓使用者明確核可才執行）。

## Description 寫法的影響

**LLM 會不會在對的時機呼叫你的 tool，幾乎完全看 description**。原則：

| 反例 | 正例 |
|------|------|
| `"Send email"` | `"Send an email to a customer. Use when the user asks to notify someone, send an invoice, or follow up on an inquiry."` |
| `"Get user data"` | `"Look up a user's profile by their internal ID. Returns name, role, last_login. Use when the user asks 'who is X' or needs to verify identity."` |
| `"Run query"` | `"Execute a read-only SQL SELECT against the analytics DB. NEVER use for INSERT/UPDATE/DELETE. Returns up to 100 rows."` |

加範例觸發詞（"use when the user asks..."）對小模型特別有用。

## Tool 回傳值

最佳實踐：永遠回 dict，含 `status` / `error` 欄位讓 LLM 判斷成功失敗：

```python
# 成功
{"status": "ok", "data": {...}}

# 失敗
{"status": "error", "error": "human-readable message"}

# 部分成功
{"status": "partial", "data": {...}, "warnings": ["..."]}
```

LLM 看到 `status: error` 會嘗試其他方式（換參數、改用其他 tool、放棄並告知使用者），比 raise exception 友善很多。

## 測試 tool

純 tool handler 是個 async function，直接測：

```python
# tests/test_tools.py
import pytest
from my_app.tools.crm import lookup_customer


@pytest.mark.asyncio
async def test_lookup_customer_returns_profile(monkeypatch):
    monkeypatch.setattr("my_app.crm_db.get", fake_get_returning_customer)
    result = await lookup_customer({"customer_id": "C-1"})
    assert result["name"] == "ACME Corp"


@pytest.mark.asyncio
async def test_lookup_customer_handles_missing_id():
    result = await lookup_customer({})
    assert "error" in result
```

整合測試（讓 LLM 真的呼叫）走 `/chat` endpoint + 觀察 SSE 流。

## 工具權限

如果某些 tool 你不想讓所有對話都能呼叫，AgenticRAG 沒有 built-in 權限層 — 在 handler 內自己擋：

```python
async def admin_only_tool(args: dict) -> dict:
    # 從 ContextVar 拿 caller 識別，或從 args 帶
    if not is_admin_call():
        return {"status": "forbidden", "error": "admin only"}
    ...
```

進階：用 anila-core framework runtime 的 per-tool permission（ALLOW/ASK/DENY），但那需要走 `/agentic-chat` 路徑。
