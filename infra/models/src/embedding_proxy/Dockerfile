FROM python:3.12-slim

RUN pip install --no-cache-dir \
    fastapi==0.115.12 \
    'uvicorn[standard]==0.32.1' \
    httpx==0.27.2

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
