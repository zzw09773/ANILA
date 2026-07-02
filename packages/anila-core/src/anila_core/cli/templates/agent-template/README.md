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

Recommended: register from the CSP UI — `/developer/agents` → **register**
(two-step wizard). Step 1 takes the agent details (name / endpoint / base model /
optional RAG collection). Step 2 issues this agent's single `csk-` service token
and shows a pre-filled `.env` snippet; paste `CSP_SERVICE_TOKEN=csk-...` into your
`.env`, start the agent, then click **test connection** to confirm the token is
wired (CSP probes your endpoint with the csk-). See
`docs/guides/developer-guide.md`.

CLI alternative (same registration, no wizard):

```bash
# Register and submit for admin approval
anila-core register --csp http://localhost:8000 --endpoint http://your-host:9100
```

Or fill in `anila.yaml` first, then run:
```bash
anila-core register
```

The `csk-` is one key: it both verifies inbound Router→agent dispatch and
authorises this agent's RAG search (CSP scopes it to the bound collection).
Unset `CSP_SERVICE_TOKEN` → the agent rejects all dispatch (fail-closed).

## Implement Your Logic

Open `agent.py` and find the `# TODO` section in `chat_completions()`.

Common patterns:
- **Simple LLM call**: use `CSPPlatformProvider` to call the main LLM through CSP
- **RAG agent**: **fork the official [`anila-agent`](../../../../../../anila-agent/) template instead** — it has the retriever options (langchain pgvector / ANILA-native pgvector / CSP HTTP search), `@anila_tool` tooling, hooks, and memory already wired up. This template is only the minimum starter.
- **Tool-calling agent**: register tools in `ToolRegistry` and use `QueryEngine`

See `examples/` in the `anila-core` repo or [`anila-agent/`](../../../../../../anila-agent/) for more complete patterns.

## Two scaffolds, two use cases

| You want to build... | Start from... |
|---|---|
| **A RAG agent** (搜文件 + 引用來源) | [`anila-agent/`](../../../../../../anila-agent/) — official template |
| **A non-RAG agent** (workflow / external API / custom logic) | **This template** (`anila-core init my-agent`) — minimal starter |

Both register to myCSPPlatform the same way — the `/developer/agents` register
wizard issues one `csk-` into `CSP_SERVICE_TOKEN` (see `.env.example`).
