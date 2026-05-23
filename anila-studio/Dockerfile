FROM python:3.11-slim AS base

# Graphviz CLI is used by diagram_renderer (subprocess `dot -Tpng`); the
# Python `graphviz` package is just a wrapper, the binary must exist.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      graphviz \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps first (Docker layer cache friendliness)
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install --no-cache-dir \
        "fastapi>=0.110,<1.0" \
        "uvicorn[standard]>=0.27" \
        "httpx>=0.27,<1.0" \
        "python-jose[cryptography]>=3.3" \
        "pydantic>=2.6,<3.0" \
        "pydantic-settings>=2.2,<3.0" \
        "redis>=5.0" \
        "cachetools>=5.3" \
        "opencc-python-reimplemented>=0.1.7"

COPY app/ ./app/

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app \
 && mkdir -p /var/anila/anila-studio-flux-cache \
 && chown anila:anila /var/anila/anila-studio-flux-cache
USER anila

EXPOSE 8100

# Container-internal health probe (docker-compose uses this for readiness).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://localhost:8100/health > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--log-level", "info"]
