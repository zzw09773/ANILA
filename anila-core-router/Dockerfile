# Build context: ANILA repo root  (docker-compose sets context: .)
# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install anila-core SDK (pure runtime — no RAG extras).
# Router only needs the runtime foundation (api, registry, tools, providers);
# RAG-specific deps (pgvector / asyncpg / pymupdf4llm) live in AgenticRAG.
COPY anila-core/pyproject.toml ./
COPY anila-core/src ./src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "."

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
