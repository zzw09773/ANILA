# syntax=docker/dockerfile:1.6
#
# Build context: the parent D:/ANILA directory, so that the sibling `anila-core/`
# package is visible during install (AgenticRAG/pyproject.toml has
# `anila-core = { path = "../anila-core" }` under [tool.uv.sources]).
#
# Build command (run from AgenticRAG/):
#     docker build -f Dockerfile -t anila-rag-sample ..
#
# Or simply:
#     docker compose build
# (docker-compose.yml sets `context: ..` automatically.)
#
# The resulting image runs `api.py` on port 24786 — an OpenAI-compatible
# chat/completions endpoint with pgvector RAG injection.

# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy both packages so the path reference resolves.
COPY anila-core/            /build/anila-core/
COPY AgenticRAG/pyproject.toml /build/AgenticRAG/
COPY AgenticRAG/api.py          /build/AgenticRAG/
COPY AgenticRAG/index_documents.py /build/AgenticRAG/

WORKDIR /build/AgenticRAG

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir /build/anila-core \
 && pip install --no-cache-dir .

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/AgenticRAG/api.py /app/api.py
COPY --from=builder /build/AgenticRAG/index_documents.py /app/index_documents.py

RUN useradd -m -u 1000 anila && chown -R anila:anila /app
USER anila

EXPOSE 24786

CMD ["python3", "/app/api.py"]
