FROM python:3.11-slim

# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY app/ ./app/

# /share/flux is bind-mounted from the host at runtime; pre-create it
# so the bind mount has a target that ``image_store`` will mkdir into
# (mkdir(parents=True, exist_ok=True) handles the rest at runtime).
RUN mkdir -p /share/flux && \
    rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
