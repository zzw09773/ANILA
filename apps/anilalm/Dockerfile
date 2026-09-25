# ──────────────────────────────────────────────────────────────────────
# ANILALM image — Vite build → nginx static serve
#
# Multi-stage so the runtime image only carries the compiled bundle and
# nginx (≈ 30 MB) rather than the 250+ MB Node toolchain.
#
# BASE_PATH is the URL prefix where the SPA is served. Default '/anilalm/'
# matches the production layout behind the ANILA reverse proxy. Override
# at build time for environments that mount it elsewhere:
#
#     docker build --build-arg BASE_PATH=/lm/ -t anilalm .
# ──────────────────────────────────────────────────────────────────────

# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
FROM node:22-alpine AS builder

WORKDIR /app

ARG BASE_PATH=/anilalm/
ENV BASE_PATH=${BASE_PATH}

# Copy lockfile first for cache-friendly installs.
# 鎖檔是必要輸入,不是可選的:原本寫 `package-lock.json*`,那個 `*` 讓「鎖檔
# 不在建置脈絡裡」變成靜默成功(照樣 build,直接跟 registry 要當下最新版)。
COPY package.json package-lock.json ./
# `npm ci`(不是 `npm install`):install 在 package.json 與鎖檔不一致時會**重新
# 解析並改寫鎖檔**,凍結後那等於映像裡裝了什麼沒有人決定過;ci 則直接失敗。
# 代價是改 package.json 之後要在 host 端跑一次 `npm install` 更新鎖檔再 build。
RUN npm ci --no-audit --no-fund \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY tsconfig*.json vite.config.ts index.html ./
COPY src ./src
COPY public ./public

RUN npm run build \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid


# ── Runtime ───────────────────────────────────────────────────────────
# nginx 版本＋digest 都釘死(2026-08-08,氣隙凍結前)。1.27 線已於 2025-06-24
# EOL,現行穩定線是 1.30;digest 是 manifest-list 的那一個,與 compose 的
# nginx(infra/compose/platform.yml)同一個 image:
#   docker buildx imagetools inspect nginx:1.30.4-alpine
# ⚠ 換版本時 tag 與 digest 要一起換 —— 不一致時實際拉的是 digest。
FROM nginx:stable-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c AS runtime

USER root
RUN apk update && apk upgrade --no-cache libuuid \
    && rm -rf /var/cache/apk/* /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# Use wget (busybox-bundled in alpine) to hit /health — nginx's own
# `service nginx status` style isn't available in the alpine image.
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/health >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
