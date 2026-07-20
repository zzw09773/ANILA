#!/usr/bin/env bash
# Generate TypeScript types from anila-studio's OpenAPI schema.
#
# Run after anila-studio's schema changes:
#   bash scripts/gen-studio-types.sh
#
# TODO: wire a GitHub Actions job that runs:
#     npm ci && npm run gen:studio-types && \
#     git diff --exit-code src/api/studio-types.gen.ts
#   so a stale generated file (i.e. a schema drift) breaks CI. Skipped
#   here because the monorepo has no central .github/workflows entrypoint
#   covering ANILALM yet.
#
# TODO: once anila-studio CI publishes the JSON as an artifact (or to a
# shared schema registry), point ANILA_STUDIO_OPENAPI at the artifact and
# drop the relative-path lookup.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANILALM_ROOT="$SCRIPT_DIR/.."
ANILA_STUDIO_OPENAPI="$ANILALM_ROOT/../../services/anila-studio/openapi/studio.openapi.json"
OUT="$ANILALM_ROOT/src/api/studio-types.gen.ts"

if [ ! -f "$ANILA_STUDIO_OPENAPI" ]; then
  echo "ERROR: anila-studio OpenAPI not found at $ANILA_STUDIO_OPENAPI" >&2
  echo "Run from the services/anila-studio root:" >&2
  echo "  .venv/bin/python -c \"from app.main import app; import json; json.dump(app.openapi(), open('openapi/studio.openapi.json','w'), indent=2, sort_keys=True, ensure_ascii=False)\"" >&2
  exit 1
fi

cd "$ANILALM_ROOT"
OPENAPI_ARG="$ANILA_STUDIO_OPENAPI"
OUT_ARG="$OUT"
# On Windows, `bash` may be WSL while `npx` resolves to the host executable.
# Windows Node cannot resolve `/mnt/c/...`; hand it native paths explicitly.
if command -v wslpath >/dev/null 2>&1 \
  && command -v npx | grep -Eq '^/mnt/[a-zA-Z]/'; then
  OPENAPI_ARG="$(wslpath -w "$ANILA_STUDIO_OPENAPI")"
  OUT_ARG="$(wslpath -w "$OUT")"
fi
npx openapi-typescript "$OPENAPI_ARG" -o "$OUT_ARG"
echo "wrote $OUT"
