FROM python:3.12-slim

RUN pip install --no-cache-dir \
    fastapi==0.115.12 \
    'uvicorn[standard]==0.32.1' \
    httpx==0.27.2 \
    numpy==1.26.4 \
    'tritonclient[grpc]==2.58.0'

WORKDIR /app
COPY app.py /app/app.py

RUN groupadd --system --gid 10001 anila-embed \
    && useradd --system --uid 10001 --gid 10001 \
        --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
        anila-embed

ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
USER 10001:10001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
