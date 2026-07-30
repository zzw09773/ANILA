#!/usr/bin/env bash
# 在既有 throwaway Postgres 上建 scratch DB → alembic upgrade head → stdout 印 DSN。
# 不動 docker、不碰 anila-restart-csp-db-1。銷毀用 --destroy。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CSP="$ROOT/services/csp"

SCRATCH_HOST="${SCRATCH_HOST:-127.0.0.1}"
SCRATCH_PORT="${SCRATCH_PORT:-55441}"
SCRATCH_USER="${SCRATCH_USER:-postgres}"
SCRATCH_PASSWORD="${SCRATCH_PASSWORD:-x}"
SCRATCH_DB="${SCRATCH_DB:-anila_manual_checks_scratch}"

PY="${CHECKS_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in \
    "$ROOT/services/csp/.venv/bin/python" \
    "/home/c1147259/桌面/ANILA/anila-migration-20260706/ANILA/services/csp/.venv/bin/python"
  do
    if [[ -x "$candidate" ]]; then PY="$candidate"; break; fi
  done
fi
if [[ -z "${PY:-}" ]]; then
  echo "BROKEN: 找不到含 alembic/sqlalchemy/psycopg2 的 Python（設 CHECKS_PYTHON）" >&2
  exit 1
fi

export SCRATCH_HOST SCRATCH_PORT SCRATCH_USER SCRATCH_PASSWORD SCRATCH_DB
ADMIN_URL="postgresql+psycopg2://${SCRATCH_USER}:${SCRATCH_PASSWORD}@${SCRATCH_HOST}:${SCRATCH_PORT}/postgres"
TARGET_URL="postgresql+psycopg2://${SCRATCH_USER}:${SCRATCH_PASSWORD}@${SCRATCH_HOST}:${SCRATCH_PORT}/${SCRATCH_DB}"

usage() {
  echo "用法: $0 [--prepare|--destroy|--dsn]"
}

cmd="${1:---prepare}"
case "$cmd" in
  --dsn) echo "$TARGET_URL"; exit 0 ;;
  --destroy)
    "$PY" - "$ADMIN_URL" "$SCRATCH_DB" <<'PY'
import sys
from sqlalchemy import create_engine, text
admin, db = sys.argv[1], sys.argv[2]
eng = create_engine(admin, isolation_level="AUTOCOMMIT")
with eng.connect() as c:
    c.execute(text(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = :d AND pid <> pg_backend_pid()"
    ), {"d": db})
    c.execute(text(f'DROP DATABASE IF EXISTS "{db}"'))
print(f"dropped {db}", file=sys.stderr)
PY
    exit 0
    ;;
  --prepare) ;;
  -h|--help) usage; exit 0 ;;
  *) usage; exit 2 ;;
esac

"$PY" - "$ADMIN_URL" "$SCRATCH_DB" <<'PY'
import sys
from sqlalchemy import create_engine, text
admin, db = sys.argv[1], sys.argv[2]
eng = create_engine(admin, isolation_level="AUTOCOMMIT")
with eng.connect() as c:
    exists = c.execute(
        text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": db}
    ).scalar()
    if exists:
        c.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :d AND pid <> pg_backend_pid()"
        ), {"d": db})
        c.execute(text(f'DROP DATABASE "{db}"'))
    c.execute(text(f'CREATE DATABASE "{db}"'))
print(f"created {db}", file=sys.stderr)
PY

export DATABASE_URL="$TARGET_URL"
export MIGRATION_DATABASE_URL="$TARGET_URL"

# 必須把本樹 anila-core 放在 venv site-packages 之前，否則會吃到舊樹
# 已安裝、依賴 anila_security 的版本。
export PYTHONPATH="$CSP:$ROOT/packages/anila-core/src${PYTHONPATH:+:$PYTHONPATH}"

cd "$CSP"
if ! "$PY" -m alembic upgrade head; then
  echo "BROKEN: alembic upgrade head 失敗" >&2
  exit 1
fi

echo "$TARGET_URL"
