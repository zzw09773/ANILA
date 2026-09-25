# Build context: ANILA repo root  (docker-compose sets context: .)
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.13-alpine@sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a AS builder

WORKDIR /build

# Router deps (pydantic-core / cryptography / asyncpg / uvloop) ship musllinux
# wheels. Do not apk-add gcc here: the compiler package set is huge and the
# previous isolated build stalled for 16+ minutes on `apk add gcc`.

# Install anila-core SDK (pure runtime — no RAG extras).
# Router only needs the runtime foundation (api, registry, tools, providers);
# RAG-specific deps (pgvector / asyncpg / pymupdf4llm) live in AgenticRAG.
COPY packages/anila-core/pyproject.toml ./
COPY services/anila-core-router/requirements.txt ./
COPY packages/anila-core/src ./src/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install "." && \
    pip install -r requirements.txt && \
    rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.13-alpine@sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a AS runtime

WORKDIR /app

RUN apk add --no-cache curl \
    && apk upgrade --no-cache libuuid \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY --from=builder /usr/local/lib/python3.13/site-packages \
                    /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY services/anila-core-router/main.py ./

# /app/.anila 是 router session SQLite 的家（compose 掛 named volume）。目錄要在
# image 裡先建好並歸 anila，named volume 第一次建立時才會繼承這個所有權，否則
# volume 是 root 的、router 寫不進去（uid 1000）。
# /run/anila/service-clients 是 CSP 寫、router 讀的憑證目錄。所有權釘成
# 10001:10002（anila-svc-tokens），mode 2750，跟 infra/docker/csp.Dockerfile
# 相同，誰先掛上 named volume 都繼承得到。router 使用者加入該群組才能讀 0640。
# pip's vendored CycloneDX SBOM still lists msgpack 1.1.2 and setuptools 70.3.0.
# Runtime does not need pip; drop it after the setuptools/msgpack upgrade so
# Trivy does not treat those vendor copies as shipped packages.
RUN pip install --no-cache-dir --upgrade 'setuptools>=78.1.1' 'msgpack>=1.2.1' \
    && pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
    && mkdir -p /app/.anila /run/anila/service-clients \
    && addgroup -g 10002 -S anila-svc-tokens \
    && adduser -D -u 1000 anila \
    && adduser anila anila-svc-tokens \
    && chown -R anila:anila /app \
    && chown 10001:10002 /run/anila/service-clients \
    && chmod 2750 /run/anila/service-clients \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER anila

EXPOSE 9000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
