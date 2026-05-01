# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# 安裝系統依賴（antiword for .doc、gcc for asyncpg C extension）
RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 複製 pyproject.toml + 原始碼，讓 hatchling 能建構 wheel
COPY pyproject.toml ./
COPY src/ ./src/

# 安裝套件（含 [rag] extras）→ agentic_rag 會被安裝進 site-packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[rag]"

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# 執行期系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    antiword \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 從 builder 複製已安裝的所有 Python 套件（含 agentic_rag wheel）
COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 上傳目錄
RUN mkdir -p /tmp/anila_uploads

# Sprint 8 X / Phase D — service token state directory.
# Compose / k8s should mount a persistent volume here so the csk-
# token survives container restarts (otherwise every restart triggers
# a fresh bootstrap which the bsk- token has been consumed by).
RUN mkdir -p /var/lib/anila-agent

# 不以 root 執行
RUN useradd -m -u 1000 anila \
    && chown -R anila:anila /app /tmp/anila_uploads /var/lib/anila-agent
USER anila

# Phase D entrypoint: auto-runs `anila-core agent bootstrap` when the
# state file is missing AND $CSP_BOOTSTRAP_TOKEN is set.
COPY --chown=anila:anila docker/entrypoint.sh /usr/local/bin/anila-entrypoint.sh
USER root
RUN chmod +x /usr/local/bin/anila-entrypoint.sh
USER anila

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/anila-entrypoint.sh"]
CMD ["uvicorn", "agentic_rag.app_factory:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--log-level", "info"]
