> ⚠ **SUPERSEDED · 2026-08-01 P2.1**
> 本 runbook 描述的靜態 `csk-`／`bsk-`／`CSP_SERVICE_TOKEN` 上手與輪替路徑**已廢止**。
> 現行：平台派工 JWT＋JWKS 驗簽；開發者不保管長效 agent 祕密。
> 請改讀 `docs/guides/developer-guide.md`。下文僅供歷史對照，勿照做。

# Service-token cutover runbook

> Sprint 8 X / Phase A–F. Walks ops through the steps required to move
> from the legacy fleet-shared `CSP_SERVICE_TOKEN` env var to per-agent
> / per-service-client credentials managed in CSP.

The cutover is staged. Each stage is reversible until you hit step 5
("remove env var fallback"). The plan deliberately keeps the legacy
env-var alive for a full release window so a zero-pressure rollback is
always possible.

> **Current state (2026-06-08).** Per-agent credentials are now the default:
> an agent owner self-issues a single `csk-` via the `/developer/agents`
> register wizard (no bsk- exchange needed) and verifies wiring with
> `test-connection`. That one `csk-` also authorises the agent's RAG search
> (scoped to its bound collection) — no separate search token. The ops steps
> below (moving OFF the legacy fleet-shared env var) are unchanged; see
> `docs/guides/developer-guide.md` for the dev-side flow.

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

## Stage 1 — Smoke-test that the seeded fleet secret still works

Existing containers still present the host `CSP_SERVICE_TOKEN`. After
migration `0027` that secret is **also** a `service_clients` row
(`client_name='router-primary'`, `is_legacy=TRUE`), and
`verify_service_token` matches the DB **before** the env fallback.
Expect:

* the token keeps working (HTTP 200 on a service-token endpoint), and
* Signal A (`service_token_legacy_env_used`) is **already zero** —
  the DB path wins, so there is nothing to "watch decay" here.
* Signal B (active `is_legacy=TRUE` rows) is **non-zero** — that is
  the real cutover watch metric; see Stage 3.

Host ports for csp/router are not exposed on the stock compose; probe
from inside the running containers (project name `anila-restart` below —
adjust if yours differs).

```bash
# Smoke: fleet secret still admitted on router-primary via the DB path.
# Expect: 200 application/json and a model payload (not 401/403).
docker exec anila-restart-csp-1 python3 -c "
import os, httpx
r = httpx.get(
    'http://127.0.0.1:8000/api/models/router-primary',
    headers={'X-CSP-Service-Token': os.environ['CSP_SERVICE_TOKEN']},
    timeout=10,
)
print(r.status_code, r.headers.get('content-type'))
print(r.text[:200])
"

# Signal A — expect 0 on a stock post-0027 deploy (DB path wins).
docker exec anila-restart-csp-db-1 psql -U csp -d csp -c "
SELECT count(*) AS legacy_env_audit_hits
  FROM audit_logs
 WHERE action = 'service_token_legacy_env_used';
"

# Signal B — expect non-zero today (the seeded router-primary row).
docker exec anila-restart-csp-db-1 psql -U csp -d csp -c "
SELECT count(*) AS active_legacy_service_clients
  FROM service_clients
 WHERE is_active = TRUE AND is_legacy = TRUE;
SELECT id, client_name, client_type
  FROM service_clients
 WHERE is_active = TRUE AND is_legacy = TRUE;
"
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
# Look up by client_name — do not hard-code numeric id (rebuilt DBs
# may allocate a different id for the same client_name).
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

## Hazard — rotating `CSP_SERVICE_TOKEN` without the DB row

⚠ **Ordinary key rotation is a landmine after the kind gate landed.**

`GET /api/models/router-primary` admits only
`service_client` + `client_type='router'`, with `allow_legacy_env=False`
(see `services/csp/app/api/models.py`). That is deliberate: an
unattributed env match cannot prove `client_type`.

Consequence: if an operator rotates the shared secret in `.env` /
compose **without** also rotating the `service_clients` row for
`client_name='router-primary'`, then after recreate:

1. Callers present the **new** env value.
2. DB still holds the **old** hash → step 1/2 miss.
3. Step 3 env fallback matches → `identity is None`.
4. The kind gate returns **403** (previously this path returned 200).

Failure body an operator will see (exact `detail` string):

```json
{
  "detail": "GET /api/models/router-primary 要求 client_type=['router'];未歸屬的 legacy env token 無法證明 client_type"
}
```

Pinned by `test_f6_env_rotated_without_db_row_returns_403_on_router_primary`
in `services/csp/tests/test_service_principal_kind_gates.py`.

**Safe rotation while still on the fleet secret:**

1. Look up the row by `client_name='router-primary'` (not by numeric id).
2. `POST /api/service-clients/{id}/rotate` (or otherwise update that
   row's token to the new secret) **before or together with** changing
   `CSP_SERVICE_TOKEN` in `.env`.
3. Recreate the consumers that present the secret
   (`docker compose up -d`, not `restart`).

Until Signal B is zero, treating `.env` as the sole source of truth
for the fleet secret is wrong — the DB row is what
`router-primary` actually checks.

---

## Stage 3 — Wait one release window

Two-week soak is a reasonable default. During this window watch **two
independent signals**. They measure different things; zero on one does
**not** imply zero on the other.

### Signal A — unattributed env fallback (often already zero)

`service_token_legacy_env_used` / `GET /api/usage/legacy-token-stats`
only fire when verify misses every active DB row and falls through to
`settings.CSP_SERVICE_TOKEN`. Migration `0027` seeded that same secret
into `service_clients` as `router-primary` (`is_legacy=TRUE`), and the
DB path wins — so a live stack can show **zero** legacy-env audit
events for days while four services still present the shared secret.

```bash
# Useful, but NOT sufficient to remove the env fallback.
curl -s http://localhost:8000/api/usage/legacy-token-stats \
  -H "Authorization: Bearer $ADMIN_JWT"
```

### Signal B — shared secret still attributed via DB (the real gate)

While any active credential still carries `is_legacy=TRUE`, the fleet
secret (or a backfilled copy of it) is still in use as a first-class
principal. This is the signal that is **non-zero today** on a stock
deploy (`service_clients` `client_name='router-primary'`) and becomes
zero only after stage 2 rotates/revokes every legacy row:

```sql
-- Must both be 0 before stage 4. Non-zero ⇒ shared secret still live
-- via the DB path (env-fallback audit will misleadingly read 0).
SELECT count(*) AS active_legacy_service_clients
  FROM service_clients
 WHERE is_active = TRUE AND is_legacy = TRUE;

SELECT count(*) AS active_legacy_agent_credentials
  FROM agent_credentials
 WHERE is_active = TRUE AND is_legacy = TRUE;
```

Also list the remaining rows so you know what to cut over:

```sql
SELECT id, client_name, client_type
  FROM service_clients
 WHERE is_active = TRUE AND is_legacy = TRUE;

SELECT c.id, a.name, c.label
  FROM agent_credentials c
  JOIN agents a ON a.id = c.agent_id
 WHERE c.is_active = TRUE AND c.is_legacy = TRUE;
```

Stop here if your fleet has only a handful of agents and stage 2 was
clean — the legacy fallback can stay enabled forever; it just means
admins still have the option of re-using the old token for emergency
recovery.

---

## Stage 4 — Final scrub (irreversible)

⚠ **Exit criterion (both required for one full release window):**

1. Signal B: `active_legacy_service_clients = 0` **and**
   `active_legacy_agent_credentials = 0` (no DB row still embodies the
   shared secret).
2. Signal A: `service_token_legacy_env_used` / legacy-token-stats also
   zero (no host still depending on the env fallback after the DB rows
   are gone).

Do **not** treat Signal A alone as proof the shared secret is unused —
that is exactly the booby-trapped reading this runbook used to teach.

```bash
# 1. Remove CSP_SERVICE_TOKEN from CSP's .env.
sed -i.bak '/^CSP_SERVICE_TOKEN=/d' .env

# 2. Recreate CSP so the new environment is loaded. The verify path's
#    env-var fallback will now refuse to match anything (env value is
#    empty). `docker compose restart` is NOT enough — it does not
#    reload `.env` or compose changes; the fleet secret would still be
#    live in the running process.
docker compose up -d csp

# 3. Smoke-test: existing per-agent / per-client tokens still work;
#    presenting the old fleet secret now correctly fails with 401.
```

After this step, the legacy `CSP_SERVICE_TOKEN` env var is dead code
on every service. A future Sprint can delete the fallback branch in
`auth_service.verify_service_token` and the env var resolution in
`services/anila-core-router/main.py`. Until then it's harmless.

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

`packages/anila-core/src/anila_core/ingestion/` is currently consumed by
`ingestion-worker` (`errors`, `chunking_plugins`). The original Phase
J plan to delete it as a "dead orphan" turned out to be wrong — it's
load-bearing. The boundary decision (keep in anila-core as shared
lib? extract to a new package? merge into ingestion-worker?) is being
deferred to a future sprint where it can be made explicitly. No
migration concerns; the code keeps running unchanged.
