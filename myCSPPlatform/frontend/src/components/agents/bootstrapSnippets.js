// Bootstrap how-to snippets for the BootstrapHowToTabs panel.
//
// Each snippet builder takes the agent's bootstrap context and returns a
// ready-to-copy code block. We split per-language so devs forking
// AgenticRAG / writing a Python agent / writing a Node agent / using
// curl from a shell each get a tailored copy that already has their
// agent_id + endpoint_url + bsk- pre-filled.
//
// Wire-contract reference: docs/csp-agent-bootstrap-protocol.md.
// Any change here must keep the body shape ({ bootstrap_token, endpoint_url })
// identical to the agentic_rag.cli.bootstrap implementation, otherwise
// the dev's copy-pasted code won't match what the bundled CLI does.

/**
 * @typedef {object} BootstrapContext
 * @property {string} cspUrl       - CSP base URL the bsk- was issued against.
 * @property {number} agentId      - Agent's id in CSP.
 * @property {string} endpointUrl  - Agent's own endpoint URL (verified by CSP).
 * @property {string} bsk          - bsk-... bootstrap token (one-shot).
 */

const PLACEHOLDER_BSK = 'bsk-PASTE-FROM-ADMIN-UI'

/**
 * Snippet for devs forking the AgenticRAG template — no code needed,
 * just .env + docker compose.
 * @param {BootstrapContext} ctx
 */
export function agenticRagFork(ctx) {
  return `# 1. Add to .env (replace bsk- if you re-issued)
cat >> .env <<'EOF'
CSP_URL=${ctx.cspUrl}
ANILA_AGENT_ID=${ctx.agentId}
ANILA_ENDPOINT_URL=${ctx.endpointUrl}
CSP_BOOTSTRAP_TOKEN=${ctx.bsk || PLACEHOLDER_BSK}
EOF

# 2. Start. The container entrypoint runs the bundled bootstrap CLI
#    on first boot and writes the long-lived csk- to a mounted volume.
docker compose up -d

# 3. Once started, remove CSP_BOOTSTRAP_TOKEN from .env (it's been
#    consumed; CSP rejects replays).
sed -i '/CSP_BOOTSTRAP_TOKEN=/d' .env`
}

/**
 * Python snippet — reference impl using httpx. Same behaviour as the
 * bundled agentic_rag.cli.bootstrap CLI but inline so devs can adapt
 * for their own (non-AgenticRAG) Python agents.
 * @param {BootstrapContext} ctx
 */
export function python(ctx) {
  const bskLiteral = ctx.bsk || PLACEHOLDER_BSK
  return `import httpx
import json
import os
import stat
from pathlib import Path

CSP_URL      = "${ctx.cspUrl}"
AGENT_ID     = ${ctx.agentId}
ENDPOINT_URL = "${ctx.endpointUrl}"
BSK          = "${bskLiteral}"  # one-shot, paste from admin UI

resp = httpx.post(
    f"{CSP_URL.rstrip('/')}/api/agents/{AGENT_ID}/bootstrap",
    json={"bootstrap_token": BSK, "endpoint_url": ENDPOINT_URL},
    timeout=30.0,
)
resp.raise_for_status()
data = resp.json()

state_path = Path("/var/lib/anila-agent/service_token.json")
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps({
    "token": data["service_token"],
    "agent_id": AGENT_ID,
    "endpoint_url": ENDPOINT_URL,
    "csp_url": CSP_URL,
    "credential_id": data["credential_id"],
    "issued_at": data["issued_at"],
    "schema_version": 1,
}, indent=2))
os.chmod(state_path, stat.S_IRUSR | stat.S_IWUSR)
print(f"OK: csk- written to {state_path}")`
}

/**
 * Node.js snippet — fetch-based, ESM. Suits devs writing TS/JS agents.
 * @param {BootstrapContext} ctx
 */
export function node(ctx) {
  const bskLiteral = ctx.bsk || PLACEHOLDER_BSK
  return `import { writeFileSync, chmodSync, mkdirSync } from 'fs'
import { dirname, join } from 'path'

const CSP_URL      = '${ctx.cspUrl}'
const AGENT_ID     = ${ctx.agentId}
const ENDPOINT_URL = '${ctx.endpointUrl}'
const BSK          = '${bskLiteral}' // one-shot, paste from admin UI

const resp = await fetch(
  \`\${CSP_URL.replace(/\\/$/, '')}/api/agents/\${AGENT_ID}/bootstrap\`,
  {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bootstrap_token: BSK, endpoint_url: ENDPOINT_URL }),
  }
)
if (!resp.ok) {
  throw new Error(\`bootstrap failed: \${resp.status} \${await resp.text()}\`)
}
const data = await resp.json()

const statePath = '/var/lib/anila-agent/service_token.json'
mkdirSync(dirname(statePath), { recursive: true })
writeFileSync(statePath, JSON.stringify({
  token: data.service_token,
  agent_id: AGENT_ID,
  endpoint_url: ENDPOINT_URL,
  csp_url: CSP_URL,
  credential_id: data.credential_id,
  issued_at: data.issued_at,
  schema_version: 1,
}, null, 2))
chmodSync(statePath, 0o600)
console.log(\`OK: csk- written to \${statePath}\`)`
}

/**
 * Go snippet — net/http. Suits devs writing Go agents.
 * @param {BootstrapContext} ctx
 */
export function go(ctx) {
  const bskLiteral = ctx.bsk || PLACEHOLDER_BSK
  return `package main

import (
    "bytes"
    "encoding/json"
    "fmt"
    "net/http"
    "os"
)

const (
    cspURL      = "${ctx.cspUrl}"
    agentID     = ${ctx.agentId}
    endpointURL = "${ctx.endpointUrl}"
    bsk         = "${bskLiteral}" // one-shot, paste from admin UI
)

func main() {
    body, _ := json.Marshal(map[string]string{
        "bootstrap_token": bsk,
        "endpoint_url":    endpointURL,
    })

    url := fmt.Sprintf("%s/api/agents/%d/bootstrap", cspURL, agentID)
    resp, err := http.Post(url, "application/json", bytes.NewBuffer(body))
    if err != nil {
        panic(err)
    }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK {
        panic(fmt.Sprintf("bootstrap returned %d", resp.StatusCode))
    }

    var data struct {
        ServiceToken string \`json:"service_token"\`
        CredentialID int    \`json:"credential_id"\`
        IssuedAt     string \`json:"issued_at"\`
    }
    if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
        panic(err)
    }

    if err := os.MkdirAll("/var/lib/anila-agent", 0700); err != nil {
        panic(err)
    }
    state, _ := json.MarshalIndent(map[string]any{
        "token":          data.ServiceToken,
        "agent_id":       agentID,
        "endpoint_url":   endpointURL,
        "csp_url":        cspURL,
        "credential_id":  data.CredentialID,
        "issued_at":      data.IssuedAt,
        "schema_version": 1,
    }, "", "  ")
    if err := os.WriteFile("/var/lib/anila-agent/service_token.json", state, 0600); err != nil {
        panic(err)
    }
    fmt.Println("OK: csk- written")
}`
}

/**
 * curl snippet — minimal sh + jq for ops / debugging.
 * @param {BootstrapContext} ctx
 */
export function curl(ctx) {
  const bskLiteral = ctx.bsk || PLACEHOLDER_BSK
  return `#!/usr/bin/env bash
set -euo pipefail

CSP_URL='${ctx.cspUrl}'
AGENT_ID=${ctx.agentId}
ENDPOINT_URL='${ctx.endpointUrl}'
BSK='${bskLiteral}'  # one-shot, paste from admin UI

response=$(curl -sS -X POST \\
  "\${CSP_URL%/}/api/agents/\${AGENT_ID}/bootstrap" \\
  -H "Content-Type: application/json" \\
  -d "{\\"bootstrap_token\\":\\"\${BSK}\\",\\"endpoint_url\\":\\"\${ENDPOINT_URL}\\"}")

mkdir -p /var/lib/anila-agent
echo "$response" | jq -r '.service_token' > /var/lib/anila-agent/service_token.txt
chmod 0600 /var/lib/anila-agent/service_token.txt
echo "OK: csk- saved (use as X-CSP-Service-Token header)"`
}

/**
 * Build all snippets at once for the tab UI.
 * @param {BootstrapContext} ctx
 */
export function buildAllSnippets(ctx) {
  return {
    agenticRagFork: agenticRagFork(ctx),
    python: python(ctx),
    node: node(ctx),
    go: go(ctx),
    curl: curl(ctx),
  }
}
