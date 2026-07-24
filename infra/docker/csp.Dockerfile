# Build context is repo root (set in compose). Dockerfile paths are
# repo-relative — services/csp + apps/csp-governance-ui for the app,
# packages/anila-core for the central SDK the inspector endpoints need.
#
# Stage 1: Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY apps/csp-governance-ui/package.json apps/csp-governance-ui/package-lock.json* ./
RUN npm install
COPY apps/csp-governance-ui/ ./
RUN npm run build

# Stage 2: Production
FROM python:3.11-slim
WORKDIR /app

# graphviz + fonts-noto-cjk: Studio Fix 2 (2026-05-18). CSP shells out
# to `dot -Tpng` from app.services.diagram_renderer to render
# Slide.diagram_dot when image_kind='diagram'. Noto CJK is required
# so DOT graphs with Traditional Chinese node labels render glyphs
# instead of tofu boxes.
#
# Round 2 fix-up (2026-05-19): originally landed in the wrong
# Dockerfile (services/csp/Dockerfile is dead — compose uses THIS
# file via dockerfile: infra/docker/csp.Dockerfile).
RUN apt-get update && apt-get install -y --no-install-recommends \
    graphviz \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install the thin wire-contract package before its consumers.
COPY packages/anila-contracts /tmp/anila-contracts
RUN pip install --no-cache-dir /tmp/anila-contracts

# Install the thin security package before anila-core. anila-core keeps a
# compatibility dependency on it, while CSP imports anila_security directly.
COPY packages/anila-security /tmp/anila-security
RUN pip install --no-cache-dir /tmp/anila-security

# Install anila-core next (changes less often than backend code, so
# layer caching survives most builds). The package brings asyncpg +
# pgvector + the AgentScopedPgVectorStore the inspector uses, and the
# ``[rag]`` extra adds the parser stack (pymupdf4llm / python-docx /
# odfpy / striprtf / Pillow) needed by
# ``anila_core.ingestion.parsers.extract_text``, which CSP calls
# directly from POST /api/ingestion/chunking-preview.
#
# AgenticRAG/ in this branch is a pure starter template — production
# code (CSP, ingestion-worker, evaluator) imports parsers/vision from
# anila_core.* directly and does NOT install AgenticRAG at runtime.
COPY packages/anila-core /tmp/anila-core
RUN pip install --no-cache-dir '/tmp/anila-core[rag]'

# Supply-chain provenance: these unreserved internal distribution names must
# resolve only from the reviewed build context, never from a package index.
RUN python -c "\
import importlib.metadata as m,json; \
expected={'anila-contracts':'file:///tmp/anila-contracts','anila-security':'file:///tmp/anila-security','anila-core':'file:///tmp/anila-core'}; \
actual={name:json.loads(m.distribution(name).read_text('direct_url.json'))['url'] for name in expected}; \
assert actual == expected, f'internal package origin mismatch: {actual}'"

# Install Python dependencies (CSP-specific)
COPY services/csp/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional debug tooling (2026-07-24 freeze: process alive but stopped accepting
# connections; runtime pip could not install py-spy, so no stack dump).
# Default 0/false keeps prod images free of debug tools; set to 1/true for
# py-spy so a frozen event loop can be dumped immediately next time.
ARG INSTALL_DEBUG_TOOLS=0
RUN if [ "$INSTALL_DEBUG_TOOLS" = "1" ] || [ "$INSTALL_DEBUG_TOOLS" = "true" ]; then \
      pip install --no-cache-dir py-spy; \
    fi

# Copy backend code (includes scripts/: generate-jwt-keypair.py + init_db.py
# land in /app/scripts — no separate scripts COPY needed since §17.1 folded
# myCSPPlatform/scripts/ into services/csp/scripts/)
COPY services/csp/ ./
COPY infra/policy/gate2/inference-callsites.v1.json /app/policy/inference-callsites.v1.json

# Copy built frontend
COPY --from=frontend-build /build/dist /app/frontend-dist

# Download Swagger UI static files for offline use
RUN pip install --no-cache-dir requests && \
    python -c "\
import requests; \
open('app/static/swagger-ui-bundle.js','wb').write(requests.get('https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui-bundle.js').content); \
open('app/static/swagger-ui.css','wb').write(requests.get('https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui.css').content); \
print('Swagger UI downloaded')" 2>/dev/null || echo "Swagger UI download skipped (offline build)"

# Runtime is deliberately non-root.  UID/GID are fixed so the offline
# deployment script can prepare the two writable/read-sensitive bind mounts
# (ingestion uploads and JWT keys) without making them world-writable.
RUN groupadd --gid 10001 csp && \
    useradd --uid 10001 --gid csp --no-create-home --shell /usr/sbin/nologin csp && \
    # The checkout used for offline builds may be mode 0640 (root-owned).
    # Keep the application tree root-owned, but grant only the csp group the
    # read/traverse access required by the non-root runtime.  Do not make the
    # source world-readable or executable, and keep /app/secrets private.
    chgrp -R csp /app/app /app/migrations /app/scripts /app/policy /app/frontend-dist && \
    chgrp csp /app/alembic.ini && \
    find /app/app /app/migrations /app/scripts /app/policy /app/frontend-dist \
      -type d -exec chmod 0750 {} + && \
    find /app/app /app/migrations /app/scripts /app/policy /app/frontend-dist \
      -type f -exec chmod 0640 {} + && \
    chmod 0640 /app/alembic.ini && \
    mkdir -p /app/logs /app/secrets /var/anila/attachments /var/anila/ingestion-uploads /var/lib/anila/source-snapshots /var/lib/anila/artifact-blobs && \
    touch /var/anila/attachments/.volume-init /var/anila/ingestion-uploads/.volume-init /var/lib/anila/source-snapshots/.volume-init /var/lib/anila/artifact-blobs/.volume-init && \
    chown -R csp:csp /app/logs /app/secrets && \
    chown -R csp:csp /var/anila /var/lib/anila && \
    chmod 700 /app/secrets /var/anila/attachments /var/anila/ingestion-uploads /var/lib/anila/source-snapshots /var/lib/anila/artifact-blobs

ENV DATABASE_URL=postgresql://csp:csp_password@postgres:5432/csp

EXPOSE 8000

USER csp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
