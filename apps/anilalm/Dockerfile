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

FROM node:22-alpine AS builder

WORKDIR /app

ARG BASE_PATH=/anilalm/
ARG VITE_DEFAULT_CHAT_MODEL=gpt-4o-mini
ENV BASE_PATH=${BASE_PATH}
ENV VITE_DEFAULT_CHAT_MODEL=${VITE_DEFAULT_CHAT_MODEL}

# Copy lockfile first for cache-friendly installs. `npm install` (not `npm ci`)
# tolerates package.json edits without forcing a host-side lockfile sync —
# fine inside an ephemeral build stage.
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY tsconfig*.json vite.config.ts index.html ./
COPY src ./src

RUN npm run build


# ── Runtime ───────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# Use wget (busybox-bundled in alpine) to hit /health — nginx's own
# `service nginx status` style isn't available in the alpine image.
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/health >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
