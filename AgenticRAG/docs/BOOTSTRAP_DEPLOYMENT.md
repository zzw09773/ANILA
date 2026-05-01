# Service-token bootstrap deployment guide

This document covers how AgenticRAG (and any agent forked from this template)
authenticates to CSP at runtime — both on first start and after admin-driven
rotation.

> Sprint 8 X / Phase D introduced this flow. The previous fleet-shared
> `CSP_SERVICE_TOKEN` env var still works as a fallback during cutover; new
> deployments should prefer bootstrap.

---

## Concepts

| Token | Where it lives | Lifecycle |
|---|---|---|
| **`bsk-...`** (bootstrap) | `.env` `CSP_BOOTSTRAP_TOKEN` | Single-use, 15 min default TTL, consumed on first agent start |
| **`csk-...`** (service) | `/var/lib/anila-agent/service_token.json` (mode 0600) | Long-lived, rotated by admin via CSP UI |
| `CSP_SERVICE_TOKEN` (legacy) | `.env` | Fleet-shared fallback; removed after cutover |

The agent's `RotatingServiceTokenMiddleware` reads the state file at startup,
falls back to `CSP_SERVICE_TOKEN` if the file is missing, and accepts both
the current and previous tokens during the rotation grace window.

---

## Single-host docker-compose deployment

The shipped `AgenticRAG/docker-compose.yml` mounts a named volume
`anila-agent-state` at `/var/lib/anila-agent` so the `csk-` survives container
restarts. First-time bring-up:

```bash
# 1. Admin registers your agent in CSP UI (Developer Agents view).
# 2. Admin issues a bootstrap token from the agent detail panel:
#    POST /api/agents/<id>/issue-bootstrap → returns bsk-XXXX
# 3. Set the four entrypoint vars in .env:
cat >> .env <<EOF
CSP_URL=http://csp:8000
ANILA_AGENT_ID=2
ANILA_ENDPOINT_URL=http://agentic-rag:24786
CSP_BOOTSTRAP_TOKEN=bsk-XXXX-from-admin
EOF

# 4. Start. The entrypoint runs the bootstrap CLI on first boot,
#    writes /var/lib/anila-agent/service_token.json (mode 0600), then execs
#    uvicorn. The CLI lives in agentic_rag.cli.bootstrap — no anila-core
#    install required.
docker compose up -d
docker compose logs -f api | head -30
# expect: [entrypoint] running agentic_rag bootstrap CLI ...
#         OK: service token written to /var/lib/anila-agent/service_token.json

# 5. Remove CSP_BOOTSTRAP_TOKEN from .env. It's been consumed; CSP rejects
#    replays. Leaving it lying around is just one more secret to leak.
sed -i '/CSP_BOOTSTRAP_TOKEN=/d' .env
```

If you need to re-bootstrap (e.g. you nuked the volume), repeat steps 2–4.

---

## Kubernetes multi-replica deployment

Each pod gets its own state file via PVC, so each pod runs its own bootstrap.
This means **admin must issue N bootstrap tokens**, one per replica, and each
gets a distinct `--label` so the dashboard can tell them apart.

### Pre-requisites

- A storage class that supports `ReadWriteOnce` (the per-pod state file is
  not shared — each replica owns its own).
- A Kubernetes Secret containing the bootstrap token, mounted as an env var.
  Rotate / revoke the Secret as soon as the bootstrap is done.

### Sample manifest sketch

```yaml
apiVersion: apps/v1
kind: StatefulSet              # StatefulSet (not Deployment) so each pod
metadata:                      # gets a stable PVC for its state file.
  name: agentic-rag
spec:
  serviceName: agentic-rag
  replicas: 3
  selector:
    matchLabels: {app: agentic-rag}
  template:
    metadata:
      labels: {app: agentic-rag}
    spec:
      containers:
        - name: api
          image: registry.example.com/anila/agentic-rag:0.5.0
          env:
            - name: CSP_URL
              value: http://csp.csp.svc:8000
            - name: ANILA_AGENT_ID
              value: "2"
            - name: ANILA_ENDPOINT_URL
              value: "http://agentic-rag.agentic-rag.svc:24786"
            - name: ANILA_REPLICA_LABEL
              valueFrom:
                fieldRef:                          # pod ordinal → "pod-0", "pod-1", …
                  fieldPath: metadata.name
            - name: CSP_BOOTSTRAP_TOKEN            # different per pod ordinal —
              valueFrom:                           # see "issuing N tokens" below.
                secretKeyRef:
                  name: agentic-rag-bootstrap
                  key: pod-0
          volumeMounts:
            - name: state
              mountPath: /var/lib/anila-agent
  volumeClaimTemplates:
    - metadata: {name: state}
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 10Mi
```

### Issuing N bootstrap tokens

```bash
# Admin runs once per replica (--label distinguishes them):
for i in 0 1 2; do
  curl -X POST http://csp:8000/api/agents/2/issue-bootstrap \
       -H "Authorization: Bearer $ADMIN_JWT" \
       -d '{"ttl_seconds": 1800}' \
    | jq -r .bootstrap_token >tokens/pod-$i.bsk
done

kubectl create secret generic agentic-rag-bootstrap \
  --from-file=pod-0=tokens/pod-0.bsk \
  --from-file=pod-1=tokens/pod-1.bsk \
  --from-file=pod-2=tokens/pod-2.bsk
```

Each pod consumes exactly one bsk- on first start. After the rollout settles,
delete the bootstrap Secret (`kubectl delete secret agentic-rag-bootstrap`) —
the bsk- values are useless once consumed and the per-pod csk- now lives in
its PVC.

---

## Recovery scenarios

### State file got nuked (`docker volume rm` / PVC re-provision)

The agent has no token to present. CSP→agent calls return 401 from the
agent. The fix is the same as initial bring-up: admin issues a fresh bsk-
and the entrypoint re-bootstraps on next start.

### Token expired (24h grace passed after rotation)

CSP-side rotation moves the previous token into a `service_token_previous_*`
slot with a 24h grace window, but if for some reason the agent never picked
up the new token (e.g. it was offline during the grace window), the rotated
token is no longer accepted. Same recovery path as above.

### CSP unreachable during bootstrap

The entrypoint calls CSP exactly once and fails with exit code 3 if CSP can't
be reached. The container then crashloops (assuming `restart: unless-stopped`
or k8s restart policy), giving the operator a chance to fix DNS / firewall /
mTLS without losing the bsk- (which was never sent).

### Bootstrap token expired before container started

Exit code 4 with a clear message. Admin re-issues a fresh bsk- (the old
hash on `agents.bootstrap_token_hash` is overwritten — no need to clean up).

---

## Switching from legacy `CSP_SERVICE_TOKEN`

If you're upgrading an existing deployment that uses the fleet-shared env var:

1. Stop using `CSP_SERVICE_TOKEN` for new agents — bootstrap them all.
2. For each existing agent, admin clicks "Issue bootstrap token" in CSP,
   the operator updates `.env` / Secret, and the agent restarts. The
   middleware reads the new state file; the env var becomes ignored.
3. After all agents have non-legacy credentials (`is_legacy=false` in the
   admin UI), CSP can drop the env-var fallback (`scripts/cutover-step-5.sh`
   in the runbook).

See `docs/runbooks/service-token-cutover.md` for the platform-level checklist.
