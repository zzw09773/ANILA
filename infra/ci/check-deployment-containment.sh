#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 -m unittest discover \
  -s "$ROOT/infra/deployment/tests" \
  -p 'test_*.py' \
  -v
