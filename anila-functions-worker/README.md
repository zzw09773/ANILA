# anila-functions-worker

Worker stack for the ANILA Functions v1 feature (assistant-message-bound
dev-authored buttons). Three runtime services share this codebase:

| Service | Image | Trust | Networks |
|---|---|---|---|
| `anila-functions-worker-api`     | from `Dockerfile.worker_api` | trusted, hardened | `anila-internal` only |
| `anila-functions-sandbox-exec`   | from `Dockerfile.sandbox`    | untrusted          | `anila-functions-net` only |
| `anila-functions-sandbox-extract`| from `Dockerfile.sandbox`    | untrusted          | `anila-functions-extract-net` only |

Worker-api speaks HTTP to CSP and Unix-domain sockets (over shared
docker volumes `jobs-exec` / `jobs-extract`) to the sandbox daemons.
Sandboxes never share a network with worker-api — the only reachable
host inside a sandbox container is the egress proxy (exec only) or
nothing (extract).

See `docs/superpowers/specs/2026-04-28-anila-functions-design.md` for
the full design and threat model.

## Layout

```
worker_api/    FastAPI gate (CSP-facing, no user code execution)
sandbox/       Daemon + runtime wrappers (untrusted, exec user code)
shared/        Wire protocol shared by api and sandbox
tests/         pytest suite — mirror of source dirs
```

## Local dev

The expected dev loop is:

  1. Build images via the project-root `docker-compose.yml`
  2. `docker compose up anila-functions-egress anila-functions-worker-api anila-functions-sandbox-exec anila-functions-sandbox-extract`
  3. Hit worker-api at `http://anila-functions-worker-api:8000` from
     the CSP container

The Sprint 2.5 prototype gate validates capability landing; until that
passes, treat this stack as untested for the setpriv path.
