#!/usr/bin/env sh
# AgenticRAG container entrypoint.
#
# Sprint 8 X / Phase D + Phase 0 decoupling (2026-05-02):
#   bootstrap CLI now lives in agentic_rag.cli.bootstrap so devs
#   forking this template don't need anila-core installed.
#
# Behaviour:
#   1. If $ANILA_AGENT_STATE_DIR/service_token.json exists → skip
#      bootstrap and start the API. RotatingServiceTokenMiddleware
#      reads the file at process startup.
#   2. If state file is absent AND $CSP_BOOTSTRAP_TOKEN is set → run
#      ``python -m agentic_rag.cli bootstrap`` to exchange the bsk-
#      for a csk- and write the state file. Required env: $CSP_URL,
#      $ANILA_AGENT_ID, $ANILA_ENDPOINT_URL.
#   3. If state file is absent AND no bootstrap token → start anyway
#      (legacy mode: middleware falls back to $CSP_SERVICE_TOKEN env
#      var; if that is also empty, the agent runs in "local dev"
#      mode where all incoming requests pass through).
#
# This script is idempotent — safe to re-run after pod restarts.

set -e

STATE_DIR="${ANILA_AGENT_STATE_DIR:-/var/lib/anila-agent}"
STATE_FILE="${STATE_DIR}/service_token.json"

mkdir -p "${STATE_DIR}"

if [ -f "${STATE_FILE}" ]; then
    echo "[entrypoint] service token state file present at ${STATE_FILE}; skipping bootstrap"
elif [ -n "${CSP_BOOTSTRAP_TOKEN}" ]; then
    : "${CSP_URL:?CSP_URL is required when CSP_BOOTSTRAP_TOKEN is set}"
    : "${ANILA_AGENT_ID:?ANILA_AGENT_ID is required when CSP_BOOTSTRAP_TOKEN is set}"
    : "${ANILA_ENDPOINT_URL:?ANILA_ENDPOINT_URL is required when CSP_BOOTSTRAP_TOKEN is set}"

    echo "[entrypoint] running agentic_rag bootstrap CLI (csp=${CSP_URL}, agent_id=${ANILA_AGENT_ID})"
    python -m agentic_rag.cli bootstrap \
        --csp-url "${CSP_URL}" \
        --bootstrap-token "${CSP_BOOTSTRAP_TOKEN}" \
        --agent-id "${ANILA_AGENT_ID}" \
        --endpoint-url "${ANILA_ENDPOINT_URL}" \
        ${ANILA_REPLICA_LABEL:+--label "${ANILA_REPLICA_LABEL}"} \
        --state-dir "${STATE_DIR}"

    # Best-effort scrub: encourage operators to remove the bsk- after
    # first start. We can't truly scrub since env vars persist, but
    # exporting an empty value at least prevents accidental log
    # leaks downstream.
    unset CSP_BOOTSTRAP_TOKEN || true
else
    echo "[entrypoint] no state file and no CSP_BOOTSTRAP_TOKEN; falling back to env-var token (legacy)"
fi

exec "$@"
