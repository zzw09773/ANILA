# Service-token cutover runbook

> Sprint 8 X / Phase A–F. Walks ops through the steps required to move
> from the legacy fleet-shared `CSP_SERVICE_TOKEN` env var to per-agent
> / per-service-client credentials managed in CSP.

The cutover is staged. Each stage is reversible until you hit step 5
("remove env var fallback"). The plan deliberately keeps the legacy
env-var alive for a full release window so a zero-pressure rollback is
always possible.

---

## Stage 0 — Verify the migration deployed

Migration `0027_agent_credentials_and_service_clients.py` should have
already run as part of CSP startup. Confirm:

```sql
-- Should return one row per approved agent, label='legacy-fleet-shared',
-- is_legacy=TRUE.
SELECT a.id, a.name, c.label, c.is_legacy, c.service_token_issued_at
  FROM agent_credentials c
  JOIN agents a ON a.id = c.agent_id
 WHERE c.is_active = TRUE
 ORDER BY a.id;

-- Should return exactly one row, client_name='router-primary',
-- is_legacy=TRUE.
SELECT id, client_name, client_type, is_legacy
  FROM service_clients
 WHERE is_active = TRUE;
```

If the legacy CSP_SERVICE_TOKEN env var was empty at migration time,
both queries return zero rows — that's a clean install, no cutover
needed; admins issue tokens fresh per agent from day one.

---

## Stage 1 — Smoke-test that legacy traffic still works

Existing AgenticRAG containers are still running with the old env-var
token. They should keep working unchanged because Phase A's verify
path falls back to the env-var on no-DB-match and writes a
`service_token_legacy_env_used` audit event.

```bash
# Send a request via the Router. Expect 200 + a streaming response.
curl -sN -X POST http://localhost:9000/v1/chat/completions \
  -H "Authorization: Bearer $SMOKE_USER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"anila-router","messages":[{"role":"user","content":"hi"}]}' \
  | head -5

# Verify the audit log shows legacy hits — these are what we'll watch
# decay during cutover.
curl -s http://localhost:8000/api/audit-logs?action=service_token_legacy_env_used \
  -H "Authorization: Bearer $ADMIN_JWT" | jq '.[0:3]'
```

---

## Stage 2 — Cut each agent over

Each agent is independent — you can do this incrementally and at your
own pace. There are two paths depending on what runtime the agent is:

### 2a — AgenticRAG-template-based agents (Tier 2)

These agents run `anila-core` middleware so they support the full
bootstrap-then-provision flow.

```bash
# 1. Admin: issue a bsk- token for this agent.
curl -X POST http://localhost:8000/api/agents/2/issue-bootstrap \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -d '{"ttl_seconds": 1800}'
# → bootstrap_token: bsk-XXX

# 2. Operator: paste the bsk- into the agent host's .env:
#       CSP_BOOTSTRAP_TOKEN=bsk-XXX
#       CSP_URL=http://csp:8000
#       ANILA_AGENT_ID=2
#       ANILA_ENDPOINT_URL=http://my-rag:24786
#    Then restart the container. Entrypoint runs anila-core agent
#    bootstrap and writes /var/lib/anila-agent/service_token.json.

# 3. Verify is_legacy=false on the new credential.
curl -s http://localhost:8000/api/agents/2/credentials \
  -H "Authorization: Bearer $ADMIN_JWT" \
  | jq '.[] | {id, label, is_legacy, is_active, issued_at}'
```

After verification, **delete the legacy-fleet-shared credential** so
the agent only has its per-agent token:

```bash
# Find the legacy credential id
LEGACY_ID=$(curl -s http://localhost:8000/api/agents/2/credentials \
  -H "Authorization: Bearer $ADMIN_JWT" \
  | jq -r '.[] | select(.is_legacy==true and .is_active==true) | .id' | head -1)

curl -X DELETE http://localhost:8000/api/agents/2/credentials/${LEGACY_ID} \
  -H "Authorization: Bearer $ADMIN_JWT"
```

### 2b — Legacy / non-AgenticRAG agents (Tier 0)

For agents that can't run the bootstrap CLI (third-party, non-Python,
or you just want minimum disruption), use the static-issue path
instead. See [`legacy-agent-bootstrap.md`](./legacy-agent-bootstrap.md)
for the full walkthrough including multi-language code examples.

Summary:

```bash
# Admin: directly issue a long-lived csk- (no bootstrap exchange).
curl -X POST http://localhost:8000/api/agents/2/credentials/issue-static \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -d '{"label": "legacy-vendor-agent"}'
# → service_token: csk-YYY

# Operator: replace CSP_SERVICE_TOKEN env var on the agent host with
# the new csk- and restart. Wire-protocol identical, no code change
# needed.
```

Then revoke the legacy fleet-shared credential as above.

### 2c — Router

The Router has a `service_clients` row pre-backfilled (`router-primary`,
is_legacy=true). Cutting it over:

```bash
# Issue a fresh csk- and disable the legacy backfill in one rotate call.
ROUTER_ID=$(curl -s http://localhost:8000/api/service-clients \
  -H "Authorization: Bearer $ADMIN_JWT" \
  | jq -r '.[] | select(.client_name=="router-primary") | .id')

curl -X POST http://localhost:8000/api/service-clients/${ROUTER_ID}/rotate \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -d '{"grace_seconds": 86400}'
# → service_token: csk-Z (24h grace; old token still works during this window)

# Operator: set CSP_BOOTSTRAP_TOKEN=csk-Z on the Router container env,
# remove the host-level CSP_SERVICE_TOKEN, restart. Entrypoint writes
# /var/lib/anila-router/service_token.json (state file path) on first
# boot. The 24h grace covers the window between admin rotate and
# operator-side restart.
```

---

## Stage 3 — Wait one release window

Two-week soak is a reasonable default. During this window:

* Watch `/api/audit-logs?action=service_token_legacy_env_used` — should
  decay to zero as you finish stage 2 for each agent.
* Watch the dashboard's "legacy_token usage" widget (Phase E).
* If a non-zero count persists, query the audit log's
  `ip_address` field to find which host is still presenting the
  fleet-shared token, then cut that agent over.

Stop here if your fleet has only a handful of agents and stage 2 was
clean — the legacy fallback can stay enabled forever; it just means
admins still have the option of re-using the old token for emergency
recovery.

---

## Stage 4 — Final scrub (irreversible)

Once `service_token_legacy_env_used` is zero for at least one full
release window:

```bash
# 1. Remove CSP_SERVICE_TOKEN from CSP's .env.
sed -i.bak '/^CSP_SERVICE_TOKEN=/d' .env

# 2. Restart CSP. The verify path's env-var fallback will now refuse
#    to match anything (env value is empty).
docker compose restart csp

# 3. Smoke-test: existing per-agent tokens still work; legacy traffic
#    now correctly fails with 401.
```

After this step, the legacy `CSP_SERVICE_TOKEN` env var is dead code
on every service. A future Sprint can delete the fallback branch in
`auth_service.verify_service_token` and the env var resolution in
`anila-core-router/main.py`. Until then it's harmless.

---

## Rollback

Before stage 4, rollback is just "stop using per-agent tokens and
re-set `CSP_SERVICE_TOKEN` everywhere". Schema rollback (`alembic
downgrade -1`) drops the new tables and 4 columns; existing agents
keep working because `auth_service.verify_service_token` falls through
to env-var when DB lookups return None.

After stage 4, rollback is more involved: you have to re-issue a
fleet-shared token from somewhere, re-set `CSP_SERVICE_TOKEN` on
every host, and revoke all per-agent credentials. We strongly
recommend taking a database snapshot before stage 4.

---

## Boundary cleanup (deferred to Sprint 9 X)

`anila-core/src/anila_core/ingestion/` is currently consumed by
`ingestion-worker` (`errors`, `chunking_plugins`). The original Phase
J plan to delete it as a "dead orphan" turned out to be wrong — it's
load-bearing. The boundary decision (keep in anila-core as shared
lib? extract to a new package? merge into ingestion-worker?) is being
deferred to a future sprint where it can be made explicitly. No
migration concerns; the code keeps running unchanged.
