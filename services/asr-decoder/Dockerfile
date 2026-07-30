# CTranslate2 4.x(faster-whisper 的後端)需要 CUDA 12 + cuDNN 9。
# ⚠ tag 必須是 `-cudnn-`(cuDNN 9)而不是舊的 `-cudnn8-`,否則 model load
# 會在 runtime 才炸 "libcudnn_ops.so.9: cannot open shared object file"。
# cudnn-runtime 這個 variant 同時帶了 cuBLAS 與 cuDNN,兩個 CT2 都要。
# Ubuntu 24.04 內建 python3.12(對齊 pyproject 的 requires-python >=3.11)。
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS base

# - python3 / python3-venv:runtime + PEP 668 隔離(24.04 的系統 python 是
#   externally-managed,不能直接 pip install)
# - curl + ca-certificates:health probe(compose 讀它判 readiness)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN python3 -m venv /opt/venv

WORKDIR /app

# 依賴宣告的 SSOT 是 pyproject.toml(對齊 anila-studio)—— 加 runtime 相依
# 只改那一個檔,不會有 Dockerfile drift。裝 `.` 而非 `.[dev]`,pytest 不進
# runtime image。setuptools 要讀得到 package 原始碼才能算 metadata,所以
# app/ 必須在 pip install 之前 COPY。
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --upgrade pip && pip install --no-cache-dir .

# ⚠ 權重刻意不烘進 image:
#   1. air-gap bundle 要能獨立替換模型尺寸(medium ↔ large-v3)而不重 build;
#   2. image 保持可簽章、體積可控(large-v3 光權重就 ~3GB);
#   3. 內部版 / 外部版兩種部署掛的是同一份權重目錄,image 完全相同。
# 由 compose 以 volume 掛到 ASR_MODEL_DIR。
RUN mkdir -p /var/anila/asr-models

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app /var/anila/asr-models
USER anila

EXPOSE 9000

# 模型是背景載入的,ready 之前 /health 回 503 → 這裡的 start-period 只要
# 夠 uvicorn 起來即可,權重載入慢由 retries 吸收(large-v3 冷啟動可能要
# 分鐘級,retries × interval 要蓋得住)。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=20 \
  CMD curl -sf http://localhost:9000/health > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
