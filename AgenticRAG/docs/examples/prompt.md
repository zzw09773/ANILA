# System Prompt 客製化

AgenticRAG 不內建任何「正確的」system prompt — 你的 agent 是 RAG 客服還是程式分析師、語氣輕鬆還是嚴肅，都是部署層的決策。本文件示範三種常見 wiring 模式。

## 基本：每次請求都帶

最直接的方式是讓客戶端在 `ChatRequest.system_prompt` 帶。`/chat` 跟 `/agentic-chat` 都接受。

```python
import httpx

resp = httpx.post(
    "http://your-agent:24786/chat",
    json={
        "user_message": "你好",
        "system_prompt": (
            "你是 ACME 公司客服 AI。回答時：\n"
            "- 用繁體中文\n"
            "- 不確定的事情明說，不要編\n"
            "- 引用文件時用 [N] 標號"
        ),
    },
)
```

`/agentic-chat` 強制要求 `system_prompt`（沒帶會 422）— 因為 framework runtime 沒有合理的內建 RAG prompt。

## 模式 A：固定 system prompt（不讓客戶端覆寫）

如果你要鎖死 agent 的人格，在 wiring 處覆寫 `ChatRequest`：

```python
# my_app/server.py
from fastapi import Request
from agentic_rag.api.server import create_app

DEFAULT_PROMPT = """你是 ACME 客服 AI。
- 用繁體中文回答
- 引用知識庫段落時用 [N] 標號
- 不確定的事情誠實說「我不知道」
"""

app = create_app(provider=..., tool_registry=...)


@app.middleware("http")
async def force_system_prompt(request: Request, call_next):
    """攔下 /chat 與 /agentic-chat，把 system_prompt 強制覆寫。"""
    if request.url.path in ("/chat", "/agentic-chat"):
        body = await request.body()
        # 修改 body... (略，需 ASGI 改 receive 包裝)
    return await call_next(request)
```

更簡單的做法：在客戶端控制；server-side 信任。

## 模式 B：動態組成（依 request 內容變）

例如根據 ChatRequest 的 `agent_type` 切換不同 prompt：

```python
# my_app/prompts.py
PROMPTS = {
    "support": "你是客服...",
    "sales": "你是銷售助理...",
    "tech": "你是技術支援...",
}


def resolve_prompt(agent_type: str | None, fallback: str | None) -> str:
    if agent_type and agent_type in PROMPTS:
        return PROMPTS[agent_type]
    return fallback or PROMPTS["support"]
```

接 chat handler — 直接 fork AgenticRAG 改 `api/server.py` 的 `chat` 函式裡的 `request.system_prompt`，或者在中介層 wrap。

## 模式 C：與 personalization 結合

System prompt 跟使用者個人化分開組裝。AgenticRAG 預設行為：

```
[使用者背景 facts block (來自 UserContextProvider)]

[ChatRequest.system_prompt 原文]
```

兩者用空行分開。如果你要讓 facts 出現在不同位置（例如 prompt 結尾、或塞進 user message），就**不要用** AgenticRAG 內建的 prefix 方式 — 改寫 chat handler 自己控制順序。

## 多語言 / 多模式 prompt

實作建議：把 prompt template 放在版控的 `.md` 檔，runtime 載入。

```
my_app/
├── prompts/
│   ├── support_zh.md
│   ├── support_en.md
│   └── tech_zh.md
└── server.py
```

```python
# my_app/server.py
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
```

好處：
- prompt 變更走 git review，不用重新 deploy 程式
- A/B test 可以用 feature flag 切不同 prompt 檔
- 翻譯交給 i18n 工具，不混在 Python 檔裡

## 反模式（不要做）

- **system prompt 寫死在 chat handler 裡** — 改 prompt 要動程式碼、走 PR、重 deploy，太重
- **把 prompt 放 env 變數** — 多行 markdown 在 env 很難讀；env 也不適合 review diff
- **system prompt 含金鑰 / 連線字串** — agent 答錯時可能 leak（很多 LLM 會回顯 system prompt 的字串）

## 進階：thinking budget / reasoning prompt

如果你的 LLM 支援 reasoning（如 gemma 思考 模型、QwQ-style），system prompt 可以指示思考深度：

```
回答前先思考。對於需要多步驟的問題：
1. 列出已知條件
2. 拆解子問題
3. 用工具查資料
4. 整合答案

簡單問題直接答，不必鋪陳。
```

這比靠 LLM 自己判斷穩定。
