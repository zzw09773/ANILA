#!/usr/bin/env bash
# ANILA Cloud Agent environment — idempotent dependency bootstrap.
#
# Runs after the repository is checked out. Safe to re-run: every step is
# guarded so repeated invocations converge instead of duplicating work.
#
# Scope: the CPU-only per-service development experience documented in
# AGENTS.md §7 (Python services + React/Vue frontends) plus a local
# Postgres 16 + pgvector 0.8 and Redis so the CSP control-plane, its test
# suite, and the governance UI run end to end. The full GPU model stack
# (infra/models, FLUX) is intentionally out of scope — it needs GPUs that
# a Cloud Agent VM does not have.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '\n\033[1;34m[install]\033[0m %s\n' "$*"; }

# ── 1. System packages ──────────────────────────────────────────────────────
# build-essential/git/curl are usually present on the base image; postgres,
# pgvector build headers, the venv module and redis are not.
log "Installing system packages (apt)…"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
  build-essential git curl ca-certificates \
  python3-venv python3-dev \
  postgresql postgresql-contrib postgresql-server-dev-all \
  redis-server

# ── 2. pgvector >= 0.7 (halfvec) ─────────────────────────────────────────────
# The CSP schema migrates document_chunks.embedding to halfvec(4000); halfvec
# was introduced in pgvector 0.7.0. Ubuntu ships 0.6.0, so build 0.8.0 from
# source unless a >=0.7 control file is already installed.
PG_EXT_DIR="$(pg_config --sharedir 2>/dev/null)/extension"
NEED_PGVECTOR=1
if [ -f "$PG_EXT_DIR/vector.control" ]; then
  CUR_VER="$(sed -n "s/^default_version = '\\([0-9.]*\\)'.*/\\1/p" "$PG_EXT_DIR/vector.control" | head -1)"
  # sort -V: if 0.7.0 is NOT greater than CUR_VER, CUR_VER is >= 0.7.0.
  if [ -n "$CUR_VER" ] && [ "$(printf '0.7.0\n%s\n' "$CUR_VER" | sort -V | head -1)" = "0.7.0" ]; then
    NEED_PGVECTOR=0
    log "pgvector $CUR_VER already provides halfvec — skipping source build."
  fi
fi
if [ "$NEED_PGVECTOR" = "1" ]; then
  log "Building pgvector v0.8.0 from source (halfvec support)…"
  BUILD_DIR="$(mktemp -d)"
  git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git "$BUILD_DIR/pgvector"
  make -C "$BUILD_DIR/pgvector"
  sudo make -C "$BUILD_DIR/pgvector" install
  rm -rf "$BUILD_DIR"
fi

# ── 3. Python virtualenv + editable installs ─────────────────────────────────
# One shared venv covers every Python service. Install order matters: the
# internal contract/security dists and anila-core first, then each service's
# dev extras. anila-agent (openai-agents SDK) is included so the whole CSP
# suite — including the cross-package trace-emitter integration test — is green.
log "Creating/refreshing Python virtualenv (.venv)…"
if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  python3 -m venv "$REPO_ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$REPO_ROOT/.venv/bin/activate"
python -m pip install --upgrade -q pip

log "Installing anila-core + services (editable)…"
pip install -q \
  -e ./packages/anila-contracts \
  -e ./packages/anila-security \
  -e './packages/anila-core[dev,rag]'
pip install -q -e './services/ingestion-worker[dev]'
pip install -q -r services/csp/requirements-dev.txt
pip install -q -e './services/anila-studio[dev]'
pip install -q -e './packages/anila-agent[dev]'

# ── 4. Frontend dependencies (npm ci per app) ────────────────────────────────
log "Installing frontend dependencies (npm ci)…"
for dir in apps/anila-shell apps/anilalm apps/csp-governance-ui packages/ui; do
  ( cd "$REPO_ROOT/$dir" && npm ci --no-audit --no-fund )
done

log "install.sh complete."
