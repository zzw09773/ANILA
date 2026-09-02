# Build context: ANILA repo root  (docker-compose sets context: .)
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid

# Install anila-core SDK (pure runtime — no RAG extras).
# Router only needs the runtime foundation (api, registry, tools, providers);
# RAG-specific deps (pgvector / asyncpg / pymupdf4llm) live in AgenticRAG.
COPY packages/anila-core/pyproject.toml ./
COPY services/anila-core-router/requirements.txt ./
COPY packages/anila-core/src ./src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "." && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY --from=builder /usr/local/lib/python3.13/site-packages \
                    /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY services/anila-core-router/main.py ./

# /app/.anila 是 router session SQLite 的家（compose 掛 named volume）。目錄要在
# image 裡先建好並歸 anila，named volume 第一次建立時才會繼承這個所有權，否則
# volume 是 root 的、router 寫不進去（uid 1000）。
RUN mkdir -p /app/.anila \
    && useradd -m -u 1000 anila \
    && chown -R anila:anila /app \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER anila

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
