# ANILA ingestion-worker
#
# Build context is repo root (set in docker-compose.yml). Three packages
# get pip-installed in dependency order, each in its own layer so cache
# survives most code edits:
#
#   1. anila-core           — central SDK (chunkers + AgentScopedPgVectorStore)
#   2. agentic-rag[rag]     — file parser stack (pymupdf4llm, python-docx,
#                             odfpy, striprtf, Pillow). Worker calls
#                             ``ParserRegistry.parse(path)``.
#   3. ingestion-worker     — the Arq handler that wires it all together.
#
# Optional ``[docling]`` and ``[zh]`` extras of agentic-rag are NOT
# installed by default — they each pull >1 GB of model weights / Torch.
# Set ``DOC_PARSER=docling`` env to enable Docling layout-aware parsing
# after rebuilding with ``[docling]`` added below.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install in dependency order. anila-core / AgenticRAG change less often
# than worker code, so their layers stay cached on most rebuilds.
COPY anila-core /tmp/anila-core
RUN pip install /tmp/anila-core

COPY AgenticRAG /tmp/AgenticRAG
RUN pip install '/tmp/AgenticRAG[rag]'

COPY ingestion-worker /tmp/ingestion-worker
RUN pip install /tmp/ingestion-worker

# Default upload dir — overridden by env / volume mount in compose.
RUN mkdir -p /var/anila/ingestion-uploads
VOLUME ["/var/anila/ingestion-uploads"]

# Run the Arq worker. ``--watch`` is intentionally OFF here — we want
# explicit restarts on code change in dev; production would never use it.
CMD ["arq", "ingestion_worker.main.WorkerSettings"]
