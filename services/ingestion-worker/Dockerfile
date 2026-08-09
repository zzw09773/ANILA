# ANILA ingestion-worker
#
# Build context is repo root (set in docker-compose.yml). Two packages
# get pip-installed in dependency order, each in its own layer so cache
# survives most code edits:
#
#   1. anila-core[rag]      — central SDK + parser stack (pymupdf4llm,
#                             python-docx, odfpy, striprtf, Pillow) and
#                             the VisionProvider for image captioning.
#   2. ingestion-worker     — the Arq handler that wires it all together.
#
# AgenticRAG/ in this branch is a pure starter template and is NOT
# installed at runtime — production code (this worker, CSP, evaluator)
# imports parsers/vision from ``anila_core.*`` directly.
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System tools that parsers shell out to:
#   - antiword: legacy .doc (binary Word) → text, used by anila_core's
#     DocParser. Without it, .doc uploads fail at parse time with
#     "antiword is required for .doc parsing".
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword \
    && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid

# Install in dependency order. anila-core changes less often than
# worker code, so its layer stays cached on most rebuilds.
COPY packages/anila-core /tmp/anila-core
RUN pip install '/tmp/anila-core[rag]' \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY services/ingestion-worker /tmp/ingestion-worker
RUN pip install /tmp/ingestion-worker \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Default upload dir — overridden by env / volume mount in compose.
RUN mkdir -p /var/anila/ingestion-uploads \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
VOLUME ["/var/anila/ingestion-uploads"]

# ── 降權:非 root 跑 (FAKE-CONTROLS #50) ────────────────────────────────────
# 10001:10001 **必須跟 infra/docker/csp.Dockerfile 同號**。csp 寫進
# share/uploads/ingestion 的原始檔,這支 worker 要讀;worker 寫回的
# <UPLOAD_DIR>/anila-images/<doc_id>/ (handlers.py:219-260),csp 的
# image_blob 端點要讀。兩邊 uid 不同 = 影像/上傳路徑單向壞掉,而且兩個容器
# 都還是 healthy。改號碼請一併改 csp.Dockerfile 與
# infra/deployment/scripts/fix-runtime-ownership.sh。
#
# chown 這個目錄有兩個作用:(a) 沒掛 bind mount 時 VOLUME 產生的匿名 volume
# 會繼承映像裡的所有權,不會又變回 root:root;(b) 掛了 bind mount 時 host
# 端所有權蓋過映像 —— 那條路徑由上面那支部署腳本負責。
RUN groupadd --gid 10001 anila \
 && useradd --create-home --uid 10001 --gid 10001 anila \
 && chown anila:anila /var/anila/ingestion-uploads \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER anila

# Run the Arq worker. ``--watch`` is intentionally OFF here — we want
# explicit restarts on code change in dev; production would never use it.
CMD ["arq", "ingestion_worker.main.WorkerSettings"]
