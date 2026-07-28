#!/usr/bin/env bash
# Sample pg_stat_activity while a profile runs (W2-8, added for the post-fix rerun).
#
# Why this exists: the pre-fix baseline established that the RAG collapse was a
# *connection* failure, not a latency failure — two sessions stuck
# `idle in transaction` on model_registry blocked the health-check UPDATE and
# every chat behind it. Latency percentiles alone cannot tell that story, so the
# post-fix run has to show connection counts per VU level as primary evidence.
#
#   ./infra/loadtest/pg-sample.sh <label> <seconds> [interval]
#
# Writes results/pg-<label>.txt: one block per sample with counts by state, the
# longest-held transaction, and any blocking chain currently present.
set -euo pipefail

LABEL="${1:?usage: pg-sample.sh <label> <seconds> [interval]}"
SECONDS_TOTAL="${2:-60}"
INTERVAL="${3:-3}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO_ROOT/infra/loadtest/results"
CONTAINER="${ANILA_DB_CONTAINER:-anila-loadtest-csp-db-1}"
mkdir -p "$OUT"
DEST="$OUT/pg-${LABEL}.txt"

# psql runs inside the container: the DB port is published on the host but the
# superuser password is not something we want on a host command line.
q() { docker exec -i "$CONTAINER" psql -U csp -d csp -At -F'|' -c "$1" 2>/dev/null || true; }

: > "$DEST"
END=$(( $(date +%s) + SECONDS_TOTAL ))
while [ "$(date +%s)" -lt "$END" ]; do
  {
    echo "=== $(date -u +%H:%M:%S) label=$LABEL ==="
    # Counts by state, excluding this sampler's own session.
    echo "-- by state --"
    q "SELECT state, count(*), COALESCE(max(now()-xact_start)::text,'-')
         FROM pg_stat_activity
        WHERE datname='csp' AND pid <> pg_backend_pid()
        GROUP BY state ORDER BY state;"
    # Any blocked session plus who is blocking it. Empty output = no contention,
    # which is itself the result we are looking for post-fix.
    echo "-- blocking chains --"
    q "SELECT w.pid, LEFT(regexp_replace(w.query,'\s+',' ','g'),60),
              b.pid, b.state, LEFT(regexp_replace(b.query,'\s+',' ','g'),60)
         FROM pg_stat_activity w
         JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS bp(pid) ON true
         JOIN pg_stat_activity b ON b.pid = bp.pid
        WHERE w.datname='csp';"
  } >> "$DEST"
  sleep "$INTERVAL"
done
echo "$DEST"
