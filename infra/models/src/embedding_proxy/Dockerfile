FROM python:3.12-slim

# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
RUN pip install --no-cache-dir \
    fastapi==0.115.12 \
    'uvicorn[standard]==0.32.1' \
    httpx==0.27.2 \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
