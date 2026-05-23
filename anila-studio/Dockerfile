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

# Install Python deps from pyproject.toml (single source of truth) — adding
# a new runtime dep only needs a one-line change there, no Dockerfile drift.
# setuptools needs the package source to compute metadata; we COPY app/
# before pip install. [project.optional-dependencies.dev] (pytest / respx /
# fakeredis) stay out of the runtime image because we install `.` not
# `.[dev]`.
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --upgrade pip && \
    pip install --no-cache-dir .

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
