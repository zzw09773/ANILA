# Build context is repo root (set in docker-compose.yml). Dockerfile
# paths are repo-relative — myCSPPlatform/* for everything historical,
# anila-core/ for the central SDK that the inspector endpoints need.
#
# Stage 1: Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY myCSPPlatform/frontend/package.json myCSPPlatform/frontend/package-lock.json* ./
RUN npm install
COPY myCSPPlatform/frontend/ ./
RUN npm run build

# Stage 2: Production
FROM python:3.11-slim
WORKDIR /app

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
COPY anila-core /tmp/anila-core
RUN pip install --no-cache-dir '/tmp/anila-core[rag]'

# Install Python dependencies (CSP-specific)
COPY myCSPPlatform/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY myCSPPlatform/backend/ ./

# Copy built frontend
COPY --from=frontend-build /build/dist /app/frontend-dist

# Copy scripts and data directory
COPY myCSPPlatform/scripts/ /app/scripts/
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
