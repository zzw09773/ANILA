#!/usr/bin/env bash
# Sample pg_stat_activity while a load level is running.
# Reclaimed from attic W2-8; adapted to anila-restart project DB container.
#
#   ./infra/loadtest/pg-sample.sh <label> <seconds> [interval_s]
#
# Writes JSONL to infra/loadtest/results/pg-<label>.jsonl
set -euo pipefail

LABEL="${1:?label}"
SECONDS_TOTAL="${2:?seconds}"
INTERVAL="${3:-2}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${ROOT}/results"
mkdir -p "$OUT_DIR"
OUT="${OUT_DIR}/pg-${LABEL}.jsonl"
: >"$OUT"

DB_CONTAINER="${ANILA_DB_CONTAINER:-anila-restart-csp-db-1}"
DB_USER="${ANILA_DB_USER:-csp}"
DB_NAME="${ANILA_DB_NAME:-csp}"

end=$((SECONDS + SECONDS_TOTAL))
while (( SECONDS < end )); do
  ts="$(date -Is)"
  row="$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -At -F$'\t' -c "
    SELECT
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS total,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active') AS active,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle') AS idle,
      (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction') AS idle_in_xact,
      (SELECT coalesce(max(extract(epoch from now() - xact_start)),0)
         FROM pg_stat_activity WHERE datname = current_database() AND xact_start IS NOT NULL) AS max_xact_s,
      (SELECT coalesce(max(extract(epoch from now() - query_start)),0)
         FROM pg_stat_activity WHERE datname = current_database() AND state = 'active') AS max_active_s,
      pg_database_size(current_database()) AS db_bytes
  " 2>/dev/null || echo -e 'ERR\tERR\tERR\tERR\tERR\tERR\tERR')"
  IFS=$'\t' read -r total active idle iix max_xact max_active db_bytes <<<"$row"
  printf '{"ts":"%s","label":"%s","total":%s,"active":%s,"idle":%s,"idle_in_xact":%s,"max_xact_s":%s,"max_active_s":%s,"db_bytes":%s}\n' \
    "$ts" "$LABEL" "${total:-null}" "${active:-null}" "${idle:-null}" "${iix:-null}" \
    "${max_xact:-null}" "${max_active:-null}" "${db_bytes:-null}" | tee -a "$OUT" >/dev/null
  # Hard stop if the database is ballooning (owner constraint).
  if [[ "${db_bytes}" =~ ^[0-9]+$ ]] && (( db_bytes > 512*1024*1024 )); then
    echo "STOP: database grew past 512 MiB (${db_bytes} bytes)" >&2
    exit 2
  fi
  sleep "$INTERVAL"
done
echo "wrote $OUT" >&2
