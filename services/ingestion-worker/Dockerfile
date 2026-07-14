# ANILA ingestion-worker
#
# Build context is repo root (set in docker-compose.yml). Four packages
# get pip-installed in dependency order, each in its own layer so cache
# survives most code edits:
#
#   1. anila-contracts      — framework-neutral wire contracts.
#   2. anila-security       — credential crypto + outbound SSRF guard.
#   3. anila-core[rag]      — central SDK + parser stack (pymupdf4llm,
#                             python-docx, odfpy, striprtf, Pillow) and
#                             the VisionProvider for image captioning.
#   4. ingestion-worker     — the Arq handler that wires it all together.
#
# AgenticRAG/ in this branch is a pure starter template and is NOT
# installed at runtime — production code (this worker, CSP, evaluator)
# imports parsers/vision from ``anila_core.*`` directly.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System tools that parsers shell out to:
#   - antiword: legacy .doc (binary Word) → text, used by anila_core's
#     DocParser. Without it, .doc uploads fail at parse time with
#     "antiword is required for .doc parsing".
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword \
    && rm -rf /var/lib/apt/lists/*

# Install in dependency order. Shared packages change less often than
# worker code, so their layers stay cached on most rebuilds.
COPY packages/anila-contracts /tmp/anila-contracts
RUN pip install /tmp/anila-contracts

COPY packages/anila-security /tmp/anila-security
RUN pip install /tmp/anila-security

COPY packages/anila-core /tmp/anila-core
RUN pip install '/tmp/anila-core[rag]'

COPY services/ingestion-worker /tmp/ingestion-worker
RUN pip install /tmp/ingestion-worker

# Default upload dir — overridden by env / volume mount in compose.  UID/GID
# 10001 intentionally matches the production CSP image because both services
# share the same upload inode; the deployment scripts prepare that surface for
# this numeric identity before either container starts.
RUN groupadd --gid 10001 ingestion \
    && useradd --create-home --uid 10001 --gid 10001 ingestion \
    && mkdir -p /var/anila/ingestion-uploads \
    && chown -R ingestion:ingestion /var/anila/ingestion-uploads \
    && chmod 700 /var/anila/ingestion-uploads
VOLUME ["/var/anila/ingestion-uploads"]

# Run the Arq worker. ``--watch`` is intentionally OFF here — we want
# explicit restarts on code change in dev; production would never use it.
USER ingestion

CMD ["arq", "ingestion_worker.main.WorkerSettings"]
