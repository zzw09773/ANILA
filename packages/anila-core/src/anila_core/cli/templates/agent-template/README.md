# {{AGENT_DISPLAY_NAME}}

An ANILA agent built with `anila-core`.

## Quickstart

```bash
# 1. Copy and fill in environment variables
cp .env.example .env

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run locally (dev mode, explicit auth bypass)
API_DEV_MODE=true uvicorn agent:app --reload --port 9100

# 4. Test the health endpoint
curl http://localhost:9100/health

# 5. Send a test request (dev mode only — production needs a Bearer dispatch JWT)
curl -X POST http://localhost:9100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}],"stream":false}'
```

## Inbound auth (P2.1)

CSP Router→agent dispatch carries a short-lived RS256 JWT:

```
Authorization: Bearer <jwt>
```

This template mounts `DispatchIdentityMiddleware`, which verifies the JWT
against `{CSP_BASE_URL}/.well-known/jwks.json` (`iss=anila-csp`,
`aud=anila-agent`). Identity (`user_id` / `department` / `agent_id`) comes
from verified claims — **not** from plaintext `X-ANILA-User-*` headers.

Unset / blank `CSP_BASE_URL` (no JWKS) → every non-public request is
**rejected** (fail-closed). There is no open door when auth is misconfigured.

For a single-file offline copy of the verifier (stdlib + cryptography), see
`anila_core/contrib/anila_verify.py`. Its `fetch_jwks` requires https; for
offline/dev without https CSP, pre-fetch JWKS and pass `jwks=` to
`verify_authorization` (no network).

## Register with ANILA Platform

Recommended: register from the CSP governance console (治理中心) —
`/developer/agents` → **register** (two-step wizard). Step 1 takes the agent
details (name / endpoint / base model / optional RAG collection). After the
agent is approved, CSP dispatches with a Bearer JWT; point `CSP_BASE_URL` at
the platform and (on CSPKI intranet) set `ANILA_CA_FILE` to the CA bundle.
See `docs/guides/developer-guide.md`.

CLI alternative (same registration, no wizard):

```bash
# Register and submit for admin approval
anila-core register --csp http://localhost:8000 --endpoint http://your-host:9100
```

Or fill in `anila.yaml` first, then run:
```bash
anila-core register
```

Agent→CSP callbacks (RAG search) reuse the inbound dispatch JWT
(`Authorization: Bearer`). Do not set `CSP_SERVICE_TOKEN` / `csk-` —
agent credential issuance is retired (P2.1).

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

Both register to the CSP platform the same way; inbound dispatch auth is the
JWKS-verified Bearer JWT described above.
