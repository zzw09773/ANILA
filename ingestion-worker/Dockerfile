# ANILA ingestion-worker
#
# Lean image: just python + the worker + anila-core. No system deps beyond
# what asyncpg / pypdf already pull. We mount the upload dir from the host
# so the CSP container and the worker share files without duplication.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install anila-core first so its layer is cached separately from worker
# code that changes more often. Both packages are copied in editable mode
# because we want code changes (during dev) to land without rebuilds; the
# image is a development-shape one for now, not optimised for prod size.
COPY anila-core /tmp/anila-core
COPY ingestion-worker /tmp/ingestion-worker

RUN pip install /tmp/anila-core /tmp/ingestion-worker

# Default upload dir — overridden by env / volume mount in compose.
RUN mkdir -p /var/anila/ingestion-uploads
VOLUME ["/var/anila/ingestion-uploads"]

# Run the Arq worker. ``--watch`` is intentionally OFF here — we want
# explicit restarts on code change in dev; production would never use it.
CMD ["arq", "ingestion_worker.main.WorkerSettings"]
