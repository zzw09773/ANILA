# AgenticRAG — ANILA Agent Template

ANILA 平台的**官方 sub-agent 模板**。Fork 本目錄、改邏輯、`docker compose up -d`，即可註冊進 [myCSPPlatform](../myCSPPlatform/) 被 Router 分派流量；也能脫離 ANILA 獨立部署，對接 OpenWebUI 或任何 OpenAI-compatible client。

```bash
git clone <your-fork>/AgenticRAG && cd AgenticRAG
cp .env.example .env       # 填 LLM_URL / EMBEDDING_URL / DATABASE_URL
docker compose up -d
curl http://localhost:24786/health   # → {"status":"ok"}
```

> **Phase 1 = AgenticRAG sub-agent template**（你在這）。Phase 2 = anila-core 主腦未來再做。

---

## 為什麼 fork 這個

AgenticRAG 是一份**自包**的 agent 模板：

- 整套 agent runtime（Action / Agent / Runner / Middleware / StateMachine / Memory / Coordinator / BG Task / Skill / MCP）vendored 在 `agentic_rag/runtime/framework/`
- **零** ANILA-internal 套件依賴（`anila-core`、`anila-agent-framework` 都不裝）
- 第三方 OSS 隨你用（langchain、llama-index、sentence-transformers …）—只是別把 anila-* 內部套件拉進來
- RAG 工具現成（vector_search / keyword_search / read_document）+ 完整 ingestion 管線
- 跑在你自己的 vLLM / NIM / TGI / Ollama 後面，**完全本地**，不打外網

---

## Architecture in 30 seconds

```
┌─────────────────────────────────────────────────────────────────┐
│  agentic_rag/runtime/framework/   ← 47 modules, vendored, MIT   │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌──────────────┐  │
│  │ Action   │→ │  Agent   │→ │  Runner    │→ │ StreamEvent  │  │
│  │ (frozen) │  │ (frozen) │  │ .run()     │  │ async iter   │  │
│  └──────────┘  └──────────┘  │ .stream()  │  └──────────────┘  │
│                              │ .resume()  │                     │
│                              └────────────┘                     │
│        ↓                          ↓                  ↓          │
│  Middleware chain          StateMachine        Memory Protocol  │
│  (Trace/Cost/Guard/        (RunPhase enum,     (MessageHistory  │
│   ShellHook/Retry/         RunSerializer,      + SemanticMemory │
│   OutputTrimmer)           checkpoint+resume)  + Kind enum)     │
│                                                                  │
│  Coordinator   BgTaskRunner    SkillLoader       MCP            │
│  (worker spawn)(bg jobs)       (.md frontmatter) (subprocess)   │
└─────────────────────────────────────────────────────────────────┘
                          ↓ ↑
┌─────────────────────────────────────────────────────────────────┐
│  agentic_rag/runtime/bridge/   ← AgenticRAG-specific glue      │
│   provider_adapter / rag_actions / agent_builder / sse_runner   │
│   citation_guardrail / coordinator_bridge / semantic_memory     │
└─────────────────────────────────────────────────────────────────┘
                          ↓ ↑
┌─────────────────────────────────────────────────────────────────┐
│  agentic_rag/{api,engine,storage,ingestion,memory,compact,...}  │
│   FastAPI surface · QueryEngine (legacy) · pgvector store ·     │
│   chunker · memdir · auto_compact                               │
└─────────────────────────────────────────────────────────────────┘
```

兩個 endpoint 並存：
- **`/chat`** — 老 QueryEngine 路徑，7-stage turn loop + budget tracker（Phase 1 期間保留）
- **`/agentic-chat`** — framework 路徑，走 `bridge/sse_runner.py` → `Runner.run()`

兩者吐同一份 SSE wire format（`ServerEvent`），前端不用分。

---

## 5 分鐘上手 — 寫你第一個 tool

```python
from agentic_rag.runtime.framework import tool, ActionContext, Agent, Runner
from agentic_rag.runtime.bridge import FrameworkProviderAdapter
from typing import Annotated

@tool
async def get_weather(
    ctx: ActionContext,
    city: str,
    units: Annotated[str, "celsius or fahrenheit"] = "celsius",
) -> dict:
    """Look up current weather for a city.

    Args:
        city: City name (e.g. "Taipei").
        units: Temperature unit.
    """
    # ... your retrieval / API logic here ...
    return {"city": city, "temp": 24, "units": units}

# Wire into an agent
adapter = FrameworkProviderAdapter(my_existing_provider)
agent = Agent(
    name="weather-bot",
    instructions="Use get_weather to answer weather questions.",
    provider=adapter,
    model="google/gemma4",
    actions=(get_weather,),
)
result = await Runner().run(agent, "What's the weather in Taipei?")
print(result.final_output)
```

`@tool` 自動從 type hints + Google-style docstring `Args:` 區塊產 JSON schema、Action name、description。不用手寫 schema。

---

## 主要能力

| 想做的事 | 用什麼 |
|---|---|
| 寫 LLM 可呼叫的 tool | `@tool` 裝飾器（auto schema）|
| 追蹤每個 tool 呼叫 | `TraceMiddleware(InMemoryBackend())` |
| 計算 token / cost | `CostMiddleware(CostTracker())` — token 永遠記，dollar 看 model 是否在 PriceTable |
| 強制答案要 cite 來源 | `enforce_citations(run_result, mode='warn')` |
| Tool output 太大塞爆 context | `ToolOutputTrimmerMiddleware(max_chars=2000)` |
| 重試 flaky tool | `RetryMiddleware(RetryPolicy(max_attempts=3), on_exceptions=(ConnectionError,))` |
| Shell hook 對每次 tool 呼叫做 audit | `ShellHookMiddleware(when='before', command=['./audit.sh'])` |
| Pod 重啟接續 run | `RunSerializer.dump(state)` → 寫檔 → restart → `Runner().resume_from_state(state, agents)` |
| Self-RAG critique loop | `Agent(reflection_enabled=True)` |
| 結構化輸出 | `Agent(output_type=MyPydanticModel)` |
| LLM spawn N 個平行 sub-agent | `Coordinator + make_coordinator_actions(coord)` |
| 長時背景任務（ingest / reindex） | `ActionKind.BG_TASK` + `BgTaskRunner` |
| 非工程師加 tool | drop `*.md` 檔到 `~/.agentic-rag/skills/` → `load_skills_from_dir()` |
| 接 `mcp-server-filesystem` / `mcp-server-github` | `MCPServer` + `MCPClient`（需 `pip install 'agentic-rag[mcp]'`）|
| 對話太長自動壓縮 | `compact.trigger_compaction.run_compaction()` + `ModelWindowTable` |

完整教學：[CSP UI → Developer Guide](https://your-csp-host/dev/guide)，或本地 fork 直接讀 `agentic_rag/runtime/framework/__init__.py` 的 docstring。

---

## RAG 內建工具（已經寫好的）

```python
from agentic_rag.runtime.bridge import build_rag_agent

agent = build_rag_agent(
    name="rag-bot",
    instructions="Answer questions using the search tool.",
    provider=adapter,
    model="google/gemma4",
    store=my_pgvector_store,
    embedder=my_embed_fn,
    reranker=my_reranker,    # optional
)
# Agent 自動帶 vector_search / keyword_search / read_document
```

每個 tool 回傳的 `Citation` 帶 `chunk_id / document_title / heading_path / page / confidence`，前端可直接 render「書名 › 章 › 節 (p.3) 87%」。

---

## Memory（已 ported 自 claude-code）

```
agentic_rag/memory/        — extract_memories / session_memory / memdir / consolidation / relevance_selector
agentic_rag/compact/       — auto_compact / sliding_window / micro_compact / trigger_compaction
```

Framework 端 `runtime.framework.memory` 暴露 `MessageHistory` + `SemanticMemory` 兩個 Protocol；`runtime.bridge.semantic_memory_bridge.MemdirSemanticMemory` 把上面整套接成 framework 介面。

```python
from agentic_rag.runtime.bridge import MemdirSemanticMemory
from agentic_rag.memory.relevance_selector import ModelBasedRelevanceSelector

selector = ModelBasedRelevanceSelector(provider, model="haiku-local")
memory = MemdirSemanticMemory(memory_dir="/var/agent/memory", relevance_selector=selector)
hits = await memory.recall("user preferences for terse answers", limit=3)
```

---

## 快速啟動（Docker）

```bash
cp .env.example .env

# 必填：
#   LLM_URL=https://your-vllm:8000/v1
#   EMBEDDING_URL=https://your-tei:8001/v1
#   DATABASE_URL=postgresql://agentic:agentic@db:5432/agentic_rag
#   API_DEV_MODE=true                     # 本地開發；上正式環境改填 API_KEY=...

docker compose up -d
docker compose logs -f api | head -30     # 看 entrypoint 是否 OK
curl http://localhost:24786/health        # → {"status":"ok"}
```

可選 extras：

```bash
pip install 'agentic-rag[rag]'      # 文件解析 + pgvector
pip install 'agentic-rag[openai]'   # framework 內建 OpenAICompatProvider（裝 openai SDK）
pip install 'agentic-rag[mcp]'      # MCP client（接外部 MCP server）
pip install 'agentic-rag[zh]'       # 繁中分詞（CKIP；~2GB 模型）
pip install 'agentic-rag[docling]'  # IBM Docling parser（layout-aware）
```

---

## API 端點

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` | discovery + health probe（公開） |
| `POST` | `/chat` | legacy QueryEngine SSE stream |
| `POST` | `/agentic-chat` | framework Runner SSE stream（推薦新 fork 用這個） |
| `GET` | `/sessions/{id}/away_summary` | away recap |
| `POST` | `/sessions/{id}/compact` | manual compact trigger |
| `POST` | `/documents/upload` | RAG ingestion |
| `POST` | `/search` | 純檢索（不過 LLM） |

ANILA / CSP-platform 對接所需的 OpenAI-compatible 端點（`/v1/models` / `/v1/chat/completions`）由 `api.py` 提供（套在 `app_factory:app` 之上）。

---

## SSE 事件 schema

```json
event: message_delta
data: {"type":"message_delta","session_id":"s1","payload":{"text":"...","turn_index":0}}

event: tool_call_started
data: {"type":"tool_call_started","payload":{"tool_call_id":"c1","tool_name":"vector_search","input":{"query":"..."}}}

event: tool_call_finished
data: {"type":"tool_call_finished","payload":{"tool_call_id":"c1","is_error":false,"output_preview":"..."}}

event: usage_update
data: {"type":"usage_update","payload":{"input_tokens":123,"output_tokens":45,"turn_count":2}}

event: stream_done
data: {"type":"stream_done","payload":{"status":"completed"}}
```

完整 schema：`agentic_rag/api/events.py`。

---

## ANILA / CSP 平台對接

1. CSP UI（Developer → Agents）下載 template / 註冊本 agent
2. CSP 發 bootstrap token（`bsk-...`），單次使用，15 分鐘 TTL
3. 你的 `.env` 設 `CSP_BOOTSTRAP_TOKEN=bsk-XXXX` + `CSP_URL=...` + `ANILA_AGENT_ID=...`
4. `docker compose up -d` 第一次啟動時 entrypoint 會自動跑 bootstrap CLI，換成長期 service token（`csk-...`）寫到 `/var/lib/anila-agent/service_token.json`
5. 之後刪掉 `.env` 裡的 `CSP_BOOTSTRAP_TOKEN`（已經被消費掉了，留著就是多一個 secret）
6. CSP 管理員 approve agent → router 開始把流量導過來

完整流程：[`docs/BOOTSTRAP_DEPLOYMENT.md`](./docs/BOOTSTRAP_DEPLOYMENT.md) · [`docs/CSP_INTEGRATION.md`](./docs/CSP_INTEGRATION.md)

---

## Fork 到你自己的 Agent

最少改三個地方：

1. **`agentic_rag/tools/`** — 加你自己的 `@tool` 函式（auto schema），或寫 `Action(...)` 手動
2. **system prompt** — 透過 API 請求的 `system_prompt` field 帶入；或在 `bridge/agent_builder.py` 改 `instructions`
3. **`anila-agent.yaml`** — 改 name / description / capabilities，註冊到 CSP

進階：替換 `LLMProvider`、加 Middleware、開 BG_TASK / Coordinator / Skill / MCP，看 [Developer Guide](https://your-csp-host/dev/guide)。

---

## 測試

```bash
pip install -e '.[rag,dev]'
pytest                                    # 632/633（striprtf optional dep 那 1 個跳過）
pytest --cov=agentic_rag --cov-report=term-missing
mypy src/agentic_rag/runtime/             # strict mode, 47 source files
ruff check src/ tests/
```

---

## Release Notes

**v0.4.0 (2026-05-02)** — 8 sprint 一次推完
- v0.1 framework 全 surface 落地：Action / Agent / Runner / Middleware (Trace/Cost/Guardrail/ShellHook/Retry/OutputTrimmer) / StateMachine + checkpoint / Memory primitive / Coordinator + worker spawn / BG_TASK runtime / Skill loader / MCP integration
- `@tool` decorator + structured output via Pydantic + `Runner.stream()` async generator
- Pod-restart resilience：`RunSerializer` checkpoint / resume；persistent extraction cursor
- Citation guardrail（`enforce_citations()`）
- Self-RAG REFLECTING phase（opt-in）
- 632 tests, mypy strict clean, ruff clean

**v0.3.x** — Phase 0 reclaimed：local copies of `pg_pool` / `pgvector_store`，零 anila-core 硬依賴

歷史細節：見 git log。

---

## License

MIT. Files inspired by [openai-agents-python](https://github.com/openai/openai-agents-python) (MIT) carry provenance headers in their docstrings.
