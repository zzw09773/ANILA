# ANILA ingestion-worker
#
# Build context is repo root (set in docker-compose.yml). Two packages
# get pip-installed in dependency order, each in its own layer so cache
# survives most code edits:
#
#   1. anila-core[rag]      — central SDK + parser stack (pymupdf4llm,
#                             python-docx, odfpy, striprtf, Pillow) and
#                             the VisionProvider for image captioning.
#   2. ingestion-worker     — the Arq handler that wires it all together.
#
# AgenticRAG/ in this branch is a pure starter template and is NOT
# installed at runtime — production code (this worker, CSP, evaluator)
# imports parsers/vision from ``anila_core.*`` directly.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install in dependency order. anila-core changes less often than
# worker code, so its layer stays cached on most rebuilds.
COPY anila-core /tmp/anila-core
RUN pip install '/tmp/anila-core[rag]'

COPY ingestion-worker /tmp/ingestion-worker
RUN pip install /tmp/ingestion-worker

# Default upload dir — overridden by env / volume mount in compose.
RUN mkdir -p /var/anila/ingestion-uploads
VOLUME ["/var/anila/ingestion-uploads"]

# Run the Arq worker. ``--watch`` is intentionally OFF here — we want
# explicit restarts on code change in dev; production would never use it.
CMD ["arq", "ingestion_worker.main.WorkerSettings"]
