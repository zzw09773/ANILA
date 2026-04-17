# Stage 1: Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Production
FROM python:3.11-slim
WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ ./

# Copy built frontend
COPY --from=frontend-build /build/dist /app/frontend-dist

# Copy scripts and data directory
COPY scripts/ /app/scripts/
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
