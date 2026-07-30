FROM python:3.11-slim
WORKDIR /app
COPY embed_stub.py /app/embed_stub.py
ENV PORT=8080 EMBED_DELAY_MS=500 EMBED_DIM=4096
EXPOSE 8080
CMD ["python3", "/app/embed_stub.py"]
