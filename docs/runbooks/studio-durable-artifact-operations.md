# Studio durable artifact operations

This runbook covers the five formal artifact pipelines: `slides`, `report`,
`mindmap`, `infographic`, and `datatable`.

## Readiness contract

Formal profiles require all of the following before Studio can become ready:

- `STUDIO_DURABLE_SUPERVISOR=true` and `STUDIO_ARTIFACT_REPORTING=true`.
- `STUDIO_ARTIFACT_SERVICE_TOKEN` and `STUDIO_RUNTIME_SERVICE_TOKEN` are both
  present and different.
- Redis is reachable and queue indexes reconcile successfully.
- The named CSP artifact writer capability probe succeeds.
- At least one durable supervisor worker is running.

`GET /health` must return HTTP 200 with `durable_jobs.ready=true`,
`artifact_reporting.ready=true`, and `job_supervisor=true`. HTTP 503 means the
instance must not receive new artifact traffic.

## Queue and retry state

Redis keys use `JOB_STORE_KEY_PREFIX` (default `anila-studio:jobs:`). Active
envelopes and indexes have no TTL. Only terminal history and DLQ envelopes use
`JOB_STORE_TTL_SECONDS`.

Each claim creates a new random lease token and increments `attempt_count`.
Every heartbeat refreshes both the Redis lease and CSP's hashed attempt/lease
admission. A stale worker cannot call retrieval, inference, or artifact upload.
After `STUDIO_JOB_MAX_ATTEMPTS`, the envelope moves to `dead_letter`; no local
fallback executes it.

## Restart recovery

1. Stop routing traffic to Studio and confirm `/health` is 503 or drained.
2. Restart Redis/Studio normally; do not delete the Redis volume.
3. Studio runs index reconciliation and reclaims expired leases.
4. For every claimed job, Studio first reads CSP ArtifactJob authority.
   If CSP already owns an artifact/version, Redis is fenced-completed from that
   authority and generation is not repeated.
5. Confirm the resulting status contains CSP `artifact_id` and `download_url`.
   Formal clients download only through CSP; Studio-local download routes are
   intentionally HTTP 410.

## Incident response

- Redis unavailable: leave Studio out of rotation; restore Redis/AOF and wait
  for reconciliation. Never switch to process-memory jobs.
- CSP unavailable: generation may not cross governed sinks and completion must
  not be reported. Restore CSP, then allow bounded retry.
- Repeated DLQ: preserve the envelope and CSP ArtifactJob, record job id,
  attempt count, trace id, task id, snapshot id, and the last error. Do not
  manually rewrite lease tokens or terminal state.
- Token rotation: rotate writer and runtime service clients separately, update
  both CSP and Studio, recreate containers, then verify readiness. The two
  tokens must never be equal and browser bearer tokens must never enter Redis.

## Required evidence

Retain the health readback, Redis AOF restart/reconcile test, fenced stale-owner
test, CSP crash-window convergence test, migration head, and a CSP-authorized
download readback for the deployment record.
