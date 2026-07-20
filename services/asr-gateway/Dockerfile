# gateway 是純 CPU:VAD 切句 + WS + JWT 驗證,沒有模型、沒有 CUDA。
# 對齊 anila-studio 的 python:3.11-slim 基底,不要用 decoder 那個 CUDA image。
FROM python:3.11-slim AS base

# curl + ca-certificates:health probe + TLS to csp(JWKS / revocations)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依賴宣告的 SSOT 是 pyproject.toml(對齊 anila-studio)。裝 `.` 而非 `.[dev]`,
# pytest 不進 runtime image。setuptools 要讀得到 package 原始碼才能算 metadata,
# 所以 app/ 必須在 pip install 之前 COPY。
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --upgrade pip && pip install --no-cache-dir .

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app
USER anila

EXPOSE 8200

# ⚠ health 路徑自帶 /asr 前綴(nginx 的 location /asr/ 不 strip prefix)。
# 打 /health 會 404 —— 那是路徑錯,不是服務掛了。
# 撤銷是 fail-closed → cache 沒 ready 時回 503,這是刻意的:ASR 那時本來就
# 不能用,health 綠燈只會讓 operator 查錯方向。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://localhost:8200/asr/health > /dev/null || exit 1

# ⚠ --workers 1 是**必要的,不是效能取捨**:per-user 併發控制(SessionRegistry)
# 放在記憶體,多 worker 會讓每個 process 各自計數 → 限制失效、「踢舊留新」
# 也踢不到別的 process 上的連線。gateway 無狀態且 CPU 輕,單 worker 夠用;
# 要 scale 必須先把計數搬到 Redis。
# --ws-ping-interval/--ws-ping-timeout:靠 uvicorn 內建的 keepalive ping 回收
# 死連線(筆電闔蓋、斷網留下的半開 TCP),gateway 自己不另外送 ping。
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8200", \
     "--workers", "1", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "20", \
     "--log-level", "info"]
