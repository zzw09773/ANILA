# Match the CUDA major.minor of the host driver (H100 typically 12.x).
# pytorch 2.6 wheels target cu124. torch 2.4 cannot infer custom_op schemas
# containing PEP 604 unions (e.g. `torch.Tensor | None`) which diffusers 0.38
# and transformers >=5.x use; bumping to torch 2.6.0 fixes both import errors.
FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip python3.11-venv \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3.11 /usr/bin/python && \
    ln -sf /usr/bin/pip3 /usr/bin/pip

WORKDIR /workspace

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu124 torch==2.6.0 && \
    pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=8 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
