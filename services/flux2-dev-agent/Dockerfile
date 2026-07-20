FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# /share/flux is bind-mounted from the host at runtime.  UID/GID 10001
# matches the application-data ownership prepared by the deployment scripts.
RUN groupadd --gid 10001 anila \
    && useradd --create-home --uid 10001 --gid 10001 anila \
    && mkdir -p /share/flux \
    && chown -R anila:anila /app /share/flux \
    && chmod 700 /share/flux

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

USER anila

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
