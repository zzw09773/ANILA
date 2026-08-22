#!/usr/bin/env bash
# ANILA Cloud Agent environment — per-boot service startup.
#
# Brings up the infrastructure the CSP control-plane and its test suite need:
# a local PostgreSQL 16 cluster (with the csp role/database and pgvector
# extension) and Redis. Idempotent: safe to run on every boot and tolerant of
# services that are already running.
set -euo pipefail

log() { printf '\n\033[1;32m[start]\033[0m %s\n' "$*"; }

DB_NAME="csp"
DB_ROLE="csp"
DB_PASSWORD="csp_password"

# ── PostgreSQL ──────────────────────────────────────────────────────────────
PG_VER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -1)"
if [ -z "$PG_VER" ]; then
  echo "[start] ERROR: no PostgreSQL cluster found; did install.sh run?" >&2
  exit 1
fi
log "Ensuring PostgreSQL $PG_VER cluster 'main' is running…"
if ! sudo pg_lsclusters -h 2>/dev/null | awk -v v="$PG_VER" '$1==v && $2=="main"{print $4}' | grep -q online; then
  sudo pg_ctlcluster "$PG_VER" main start || true
fi
# Wait for readiness.
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

log "Ensuring role/database/extension exist…"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DB_ROLE}') THEN
    CREATE ROLE ${DB_ROLE} LOGIN SUPERUSER PASSWORD '${DB_PASSWORD}';
  END IF;
END \$\$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_ROLE}" "${DB_NAME}"
fi
sudo -u postgres psql -d "${DB_NAME}" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"

# ── Redis ────────────────────────────────────────────────────────────────────
log "Ensuring Redis is running…"
if ! redis-cli ping >/dev/null 2>&1; then
  sudo redis-server /etc/redis/redis.conf --daemonize yes || redis-server --daemonize yes
fi

log "start.sh complete. Postgres: postgresql://${DB_ROLE}:${DB_PASSWORD}@localhost:5432/${DB_NAME} | Redis: localhost:6379"
