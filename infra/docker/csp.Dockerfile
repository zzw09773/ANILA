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

# Install anila-core first (changes less often than backend code, so
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

# Install Python dependencies (CSP-specific)
COPY services/csp/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code (includes scripts/: generate-jwt-keypair.py + init_db.py
# land in /app/scripts — no separate scripts COPY needed since §17.1 folded
# myCSPPlatform/scripts/ into services/csp/scripts/)
COPY services/csp/ ./

# Copy built frontend
COPY --from=frontend-build /build/dist /app/frontend-dist

RUN mkdir -p /app/logs

# Download Swagger UI static files for offline use
RUN pip install --no-cache-dir requests && \
    python -c "\
import requests; \
open('app/static/swagger-ui-bundle.js','wb').write(requests.get('https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui-bundle.js').content); \
open('app/static/swagger-ui.css','wb').write(requests.get('https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.18.2/swagger-ui.css').content); \
print('Swagger UI downloaded')" 2>/dev/null || echo "Swagger UI download skipped (offline build)"

ENV DATABASE_URL=postgresql://csp:csp_password@postgres:5432/csp

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
