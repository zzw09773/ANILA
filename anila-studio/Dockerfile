FROM python:3.11-slim AS base

# System binaries the runtime needs:
# - graphviz (subprocess `dot -Tpng/svg`): diagram_renderer + mindmap_renderer
# - pandoc: HTML → DOCX for report artifacts (pypandoc shells out to it)
# - playwright chromium dependencies: libs the headless browser needs at run-time
#   for HTML → PDF rendering (NSS, X-something, fonts, etc.)
# - fonts-noto-cjk: CJK glyph coverage for matplotlib chart labels +
#   PDF render output (報告 / 資訊圖表 內文常含 CJK)
# - curl + ca-certificates: health probe + TLS to csp
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      graphviz \
      pandoc \
      curl \
      ca-certificates \
      fonts-noto-cjk \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libxkbcommon0 libatspi2.0-0 libx11-6 libxcomposite1 libxdamage1 \
      libxext6 libxfixes3 libxrandr2 libgbm1 libcairo2 libpango-1.0-0 \
      libasound2 libdrm2 libxshmfence1 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Python deps from pyproject.toml (single source of truth) — adding
# a new runtime dep only needs a one-line change there, no Dockerfile drift.
# setuptools needs the package source to compute metadata; we COPY app/
# before pip install. [project.optional-dependencies.dev] (pytest / respx /
# fakeredis) stay out of the runtime image because we install `.` not
# `.[dev]`.
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --upgrade pip && \
    pip install --no-cache-dir . && \
    # Playwright chromium binary 必須在 pip 之後另外裝。
    # --with-deps 已在 apt 階段裝完(slim image 沒帶);這裡只下 chromium。
    playwright install chromium

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app \
 && mkdir -p /var/anila/anila-studio-flux-cache \
 && chown anila:anila /var/anila/anila-studio-flux-cache \
 # ARTIFACTS_DIR 由 docker-compose 以 named volume mount,初次 mount 是
 # root:root → anila uid 10001 寫不進。預先 mkdir + chown 確保 volume
 # owner 對齊(named volume 在 first mount 時會 inherit dir owner)。
 && mkdir -p /var/anila/anila-studio-artifacts \
 && chown anila:anila /var/anila/anila-studio-artifacts
USER anila

EXPOSE 8100

# Container-internal health probe (docker-compose uses this for readiness).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://localhost:8100/health > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--log-level", "info"]
