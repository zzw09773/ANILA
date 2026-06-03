# anila-agent — dockerized "hello world" agent service.
#
# Builds the agent + the [serving] extra and runs the OpenAI-compatible wrapper
# so the CSP Router can dispatch to it. Copy + adjust for your own agent
# (set ANILA_AGENT_NAME / ANILA_COLLECTION_ID / CSP_SERVICE_TOKEN at runtime).
#
#   docker build -t my-agent .
#   docker run --rm -p 8200:8200 --env-file .env my-agent
#
FROM python:3.11-slim

# Faster, quieter, no .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better layer caching): copy only the metadata,
# then the source. pgvector extra is omitted — the recommended retrieval path
# is CspHttpRetriever (HTTP, no DB driver needed in the agent container).
COPY pyproject.toml README.md ./
COPY anila_agent ./anila_agent
COPY configs ./configs
COPY examples ./examples
RUN pip install --upgrade pip && pip install '.[serving]'

# Non-root runtime.
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8200

# The service wrapper reads config from env at startup (see
# examples/service_wrapper.py). Override the module if your agent ships its own.
CMD ["uvicorn", "examples.service_wrapper:app", "--host", "0.0.0.0", "--port", "8200"]
