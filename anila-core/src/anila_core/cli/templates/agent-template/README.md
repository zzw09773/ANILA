# {{AGENT_DISPLAY_NAME}}

An ANILA agent built with `anila-core`.

## Quickstart

```bash
# 1. Copy and fill in environment variables
cp .env.example .env

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run locally (dev mode, no auth)
API_DEV_MODE=true uvicorn agent:app --reload --port 9100

# 4. Test the health endpoint
curl http://localhost:9100/health

# 5. Send a test query
curl -X POST http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}],"stream":false}'
```

## Register with ANILA Platform

Once your agent is running and accessible:

```bash
# Register and submit for admin approval
anila-core register --csp http://localhost:8000 --endpoint http://your-host:9100
```

Or fill in `anila.yaml` first, then run:
```bash
anila-core register
```

## Implement Your Logic

Open `agent.py` and find the `# TODO` section in `chat_completions()`.

Common patterns:
- **Simple LLM call**: use `CSPPlatformProvider` to call the main LLM through CSP
- **RAG agent**: **fork the official [`AgenticRAG`](../../../../../../AgenticRAG/) template instead** — it has ingestion pipeline, Hybrid Search, cross-encoder reranker, vision OCR, and tool-driven loop already wired up. This template is only the minimum starter.
- **Tool-calling agent**: register tools in `ToolRegistry` and use `QueryEngine`

See `examples/` in the `anila-core` repo or [`AgenticRAG/`](../../../../../../AgenticRAG/) for more complete patterns.

## Two scaffolds, two use cases

| You want to build... | Start from... |
|---|---|
| **A RAG agent** (搜文件 + 引用來源) | [`AgenticRAG/`](../../../../../../AgenticRAG/) — official template, 5 min to fork |
| **A non-RAG agent** (workflow / external API / custom logic) | **This template** (`anila-core init my-agent`) — minimal starter |

Both register to myCSPPlatform the same way: edit `anila.yaml` (manifest) and run
`anila-core register` (which logs in with username/password and posts to
`/api/agents/register` with a JWT — registration itself does **not** use
`CSP_SERVICE_TOKEN`). At runtime the agent then verifies inbound CSP traffic
via `CSP_SERVICE_TOKEN` (currently a global env shared with myCSPPlatform;
per-agent issuance UI is planned).
