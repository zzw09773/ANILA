# Legacy / non-AgenticRAG agent bootstrap

> Sprint 8 X / Phase F. For agents that **don't** run the AgenticRAG
> template's `anila-core` middleware. Three integration tiers are
> supported — pick the one that matches how much you want to change
> on the agent side.

The wire protocol is identical across all tiers: agents present
`X-CSP-Service-Token: <csk-...>` on incoming requests; CSP's verify
path looks the token up in `agent_credentials` and resolves the
caller's `agent_id`. The differences below are only about how the
token gets onto the agent host and how it stays current under
rotation.

---

## Decision tree

```
                  ┌─ Can fork AgenticRAG and run anila-core?
                  │      └─ Tier 2 (full bootstrap, auto-rotate aware)
                  │
                  ├─ Can run a 50-line poller in your language?
                  │      └─ Tier 1 (admin issues bsk-, agent polls /credentials/me)
                  │
                  └─ Want zero code change, just env var swap?
                         └─ Tier 0 (admin issues static csk-, periodic
                            manual rotate)
```

---

## Tier 0 — zero code change

Use case: third-party agent, vendor binary, agent in a language you
don't want to touch, or temporary cutover during peak hours.

### Steps

```bash
# 1. Admin: issue a static (long-lived, no bootstrap) credential.
curl -X POST http://csp:8000/api/agents/2/credentials/issue-static \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -d '{"label": "vendor-foo-prod"}'
# → service_token: csk-XYZ
# → credential_id: 17

# 2. Operator: replace the agent's CSP_SERVICE_TOKEN env var:
#       OLD: CSP_SERVICE_TOKEN=fleet-shared-old-secret
#       NEW: CSP_SERVICE_TOKEN=csk-XYZ
#    Restart the agent. Done — wire protocol identical.
```

### Trade-off

You **lose automatic rotation**. CSP can still rotate the credential
(returns a new csk- and keeps the old one valid for 24h), but the
agent host's env var won't auto-update. You need a manual rotation
schedule:

```bash
# Every 90 days, or whenever a leak is suspected:
curl -X POST http://csp:8000/api/agents/2/credentials/17/rotate \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -d '{"grace_seconds": 86400}'
# → service_token: csk-NEW
# Within 24h: operator updates env var, restarts container.
```

The dashboard surfaces credentials whose `service_token_issued_at` is
> 90 days old as "needs rotation" so admins don't have to remember.

---

## Tier 1 — minimal poller

Use case: your agent is in Go / Node / Rust / Java / etc. — anila-core
isn't an option but you can spend ~50 lines to be a good citizen.

### Pattern

1. **Bootstrap once** with a `bsk-` from admin: POST
   `/api/agents/{id}/bootstrap` → store the csk-.
2. **Poll** GET `/api/agents/{id}/credentials/me` (auth = current
   csk-) every hour. The response tells you when the credential was
   issued / rotated. If the rotation happened more recently than your
   local cache, fetch the new csk- via... well, you can't, because
   `/credentials/me` deliberately doesn't return the plaintext
   (anti-replay). So instead:
3. **On any 401 from CSP-incoming traffic** (i.e. CSP started using a
   new csk- in `X-CSP-Service-Token` headers because admin rotated),
   trigger a re-bootstrap. Admin issues a fresh bsk-, you exchange
   it for the new csk-.

A simpler alternative: **don't poll, just react to 401**. CSP's
rotation grace window is 24h, so your agent has a day to receive a
fresh bsk- from admin out-of-band. For most deployments this is
plenty.

### Reference implementations

> The full code lives in `examples/legacy-agent-snippets/` (created
> alongside this doc). Below are condensed sketches.

#### Python (no anila-core dependency)

```python
import hmac, json, os, pathlib

STATE = pathlib.Path("/var/lib/anila-agent/service_token.json")

def load_token() -> str:
    return json.loads(STATE.read_text())["token"]

def verify_incoming(header_value: str) -> bool:
    return hmac.compare_digest(header_value or "", load_token())

# In your request handler:
def handle(request):
    if not verify_incoming(request.headers.get("X-CSP-Service-Token", "")):
        return Response(status=401, body="bad service token")
    # ... do the work ...
```

After admin rotation: replace `STATE` with the new csk- (from a
fresh bootstrap), reload the next request will pick it up.

#### Go

```go
package main

import (
    "crypto/subtle"
    "encoding/json"
    "net/http"
    "os"
    "sync"
)

type tokenStore struct {
    mu    sync.RWMutex
    token string
}

func (t *tokenStore) Reload() error {
    raw, err := os.ReadFile("/var/lib/anila-agent/service_token.json")
    if err != nil {
        return err
    }
    var payload struct {
        Token string `json:"token"`
    }
    if err := json.Unmarshal(raw, &payload); err != nil {
        return err
    }
    t.mu.Lock()
    t.token = payload.Token
    t.mu.Unlock()
    return nil
}

func (t *tokenStore) Verify(header string) bool {
    t.mu.RLock()
    defer t.mu.RUnlock()
    return subtle.ConstantTimeCompare([]byte(header), []byte(t.token)) == 1
}

func main() {
    store := &tokenStore{}
    _ = store.Reload()
    http.HandleFunc("/chat", func(w http.ResponseWriter, r *http.Request) {
        if !store.Verify(r.Header.Get("X-CSP-Service-Token")) {
            // Try one hot-reload before failing — admin rotation case.
            _ = store.Reload()
            if !store.Verify(r.Header.Get("X-CSP-Service-Token")) {
                http.Error(w, "bad service token", http.StatusUnauthorized)
                return
            }
        }
        // ... do the work ...
    })
    http.ListenAndServe(":24786", nil)
}
```

#### Node.js

```javascript
import { readFileSync } from "node:fs";
import { timingSafeEqual } from "node:crypto";

const STATE = "/var/lib/anila-agent/service_token.json";
let cached = "";

function reload() {
  try {
    cached = JSON.parse(readFileSync(STATE, "utf-8")).token || "";
  } catch {
    cached = "";
  }
}
reload();

function verify(header) {
  if (!cached || !header) return false;
  const a = Buffer.from(cached);
  const b = Buffer.from(header);
  return a.length === b.length && timingSafeEqual(a, b);
}

// Inside your http handler:
//   if (!verify(req.headers["x-csp-service-token"])) {
//     reload();
//     if (!verify(req.headers["x-csp-service-token"])) return res.status(401).end();
//   }
```

### Bootstrap step (do this once on first start)

In any language:

```
POST {csp_url}/api/agents/{agent_id}/bootstrap
Content-Type: application/json
{
  "bootstrap_token": "bsk-...",
  "endpoint_url": "http://my-agent:9000",
  "label": "pod-1"
}

→ 200 {"service_token": "csk-...", "credential_id": 42, ...}
```

Write the returned `service_token` to `/var/lib/anila-agent/service_token.json`
with `mode 0600`:

```json
{
  "token": "csk-...",
  "agent_id": 2,
  "credential_id": 42,
  "schema_version": 1
}
```

### Verifying it works

```bash
# Send a fake CSP-shaped request to your agent's incoming endpoint.
curl -i -X POST http://my-agent:24786/chat \
  -H "X-CSP-Service-Token: $(jq -r .token /var/lib/anila-agent/service_token.json)" \
  -H "X-ANILA-User-Id: 1" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
# Expect 200; with the wrong token expect 401.
```

---

## Tier 2 — full anila-core middleware

This is what AgenticRAG-template-based agents use. See
[`AgenticRAG/docs/BOOTSTRAP_DEPLOYMENT.md`](../../AgenticRAG/docs/BOOTSTRAP_DEPLOYMENT.md)
for the deployment story (single-host docker-compose + K8s
multi-replica + recovery).

In short:

```python
from fastapi import FastAPI
from anila_core.api.middleware.auth import RotatingServiceTokenMiddleware

app = FastAPI()
app.add_middleware(
    RotatingServiceTokenMiddleware,
    state_dir="/var/lib/anila-agent",
)
```

`RotatingServiceTokenMiddleware` handles state-file load, env-var
fallback, and **single hot-reload after a 403** so admin rotation
propagates without a process restart. The bootstrap exchange happens
once via `anila-core agent bootstrap` (from the CLI) at first start.

---

## Choosing your tier

| Concern | Tier 0 | Tier 1 | Tier 2 |
|---|---|---|---|
| Code change on agent | None (env var swap) | ~50 LOC | install anila-core |
| Per-agent identity | ✅ | ✅ | ✅ |
| Audit attribution | ✅ | ✅ | ✅ |
| Compromise blast radius | One agent | One agent | One agent |
| Auto rotation handling | ❌ (manual env update) | Hot reload on 401 | Hot reload on 403 |
| Multi-replica K8s | Manual per-pod tokens | Manual per-pod tokens | Per-pod via PVC + bootstrap CLI |
| Recommended for | Vendor / third-party / temporary | Internal-but-non-Python | New / forked AgenticRAG |
