# gateway 是純 CPU:VAD 切句 + WS + JWT 驗證,沒有模型、沒有 CUDA。
# 對齊 anila-studio 的 python:3.13-slim 基底,不要用 decoder 那個 CUDA image。
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
FROM python:3.13-slim AS base

# curl + ca-certificates:health probe + TLS to csp(JWKS / revocations)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ⚠ **build context 是 repo 根目錄**(platform.yml / dev.yml 的 `context: ../..`),
# 不是 services/asr-gateway/ —— 因為下面要複製 packages/anila-core 的 SSRF guard。
# 對齊 services/anila-core-router 與 services/ingestion-worker 的既有做法。

# 依賴宣告的 SSOT 是 pyproject.toml(對齊 anila-studio)。裝 `.` 而非 `.[dev]`,
# pytest 不進 runtime image。setuptools 要讀得到 package 原始碼才能算 metadata,
# 所以 app/ 必須在 pip install 之前 COPY。
COPY services/asr-gateway/pyproject.toml ./
COPY services/asr-gateway/app/ ./app/
RUN pip install --upgrade pip && pip install --no-cache-dir . \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ── SSRF guard(anila_core.security)────────────────────────────────────────
# 解碼位址一旦可以指到院外,ASR 就會是平台唯一跳過 validate_outbound_url 的
# 模型呼叫。guard 必須進來。
#
# 為什麼是「複製子樹」而不是 `pip install packages/anila-core`:
#   * 整包 anila-core 會拖進 asyncpg / pgvector / aiosqlite / opencc —— 這個
#     服務刻意是「純 CPU、無 GPU、無 DB」(見 README 職責邊界),而且氣隙
#     bundle 每多一個輪子就多一份維護。
#   * 為什麼不 vendor 成 services/asr-gateway/app/url_guard.py:那會在 repo 裡
#     產生第二份安全檢查,而本服務已經吃過一次 vendored 副本的苦(見
#     app/services/jwks_client.py 的 VENDORED 標頭)。**repo 裡永遠只有一份
#     url_guard.py**,build 時複製原檔進來。
#
# ⚠ **訂正(2026-08-05):「沒有東西可以漂移」只對 git,對跑著的容器是錯的。**
#   image 裡是一份 build 時的**拷貝**,沒有 bind mount;`up -d` 不會重建 image。
#   所以「改了 packages/anila-core/.../url_guard.py → up -d → 以為新規則生效了」
#   會是靜默失敗:asr-gateway 照舊用舊規則,沒有任何錯誤訊息。csp 與 router
#   是同一個形狀,不是本服務獨有 —— 但原本的註解會讓讀的人以為不必查,那才是
#   問題。所以下面留一個指紋,查得動:
#       docker exec anila-restart-asr-gateway-1 cat /app/.url_guard.sha256
#       sha256sum packages/anila-core/src/anila_core/security/url_guard.py
#   兩邊不同 = 部署的是舊的,要 `docker compose build asr-gateway` 再 `up -d`。
#
# anila_core/__init__.py 只有 docstring(零 import);security/ 這個子套件只相依
# `cryptography`,而 python-jose[cryptography] 已經帶進來了。子套件之外的相依
# 一旦被加進去,這個 image 會在啟動 import 時當場炸 —— 那是刻意的,不要包
# try/except 吞掉。
COPY packages/anila-core/src/anila_core/__init__.py ./anila_core/__init__.py
COPY packages/anila-core/src/anila_core/security/ ./anila_core/security/
RUN python -c "from anila_core.security.url_guard import validate_outbound_url; \
from anila_core.security.upstream_urls import join_upstream_path; print('url_guard ok')" \
 && sha256sum anila_core/security/url_guard.py | cut -d' ' -f1 > /app/.url_guard.sha256 \
 && echo "url_guard sha256 = $(cat /app/.url_guard.sha256)" \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
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
