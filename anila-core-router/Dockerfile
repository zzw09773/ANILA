# Build context: ANILA repo root  (docker-compose sets context: .)
# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install anila-core SDK from monorepo source
COPY AgenticRAG/pyproject.toml ./
COPY AgenticRAG/src ./src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[rag]"

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY anila-core-router/main.py ./

RUN useradd -m -u 1000 anila && chown -R anila:anila /app
USER anila

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
