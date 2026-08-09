# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.
FROM node:22-alpine AS builder

WORKDIR /app

ARG VITE_CSP_BASE_URL=http://localhost:8000
ARG VITE_ROUTER_BASE_URL=http://localhost:9000
# Serving prefix — 正式部署 '/anila/'(443 同源 subpath,SSO cookie 自然共用);
# 不傳 = '/'(本機 dev 直跑)。
ARG BASE_PATH=/

ENV VITE_CSP_BASE_URL=${VITE_CSP_BASE_URL}
ENV VITE_ROUTER_BASE_URL=${VITE_ROUTER_BASE_URL}
ENV BASE_PATH=${BASE_PATH}

# 鎖檔是必要輸入,不是可選的:原本寫 `package-lock.json*`,那個 `*` 讓「鎖檔
# 不在建置脈絡裡」變成靜默成功(照樣 build,直接跟 registry 要當下最新版)。
COPY package.json package-lock.json ./
# `npm ci`(不是 `npm install`):install 在 package.json 與鎖檔不一致時會**重新
# 解析並改寫鎖檔**,凍結後那等於映像裡裝了什麼沒有人決定過;ci 則直接失敗。
# 代價是改 package.json 之後要在 host 端跑一次 `npm install` 更新鎖檔再 build。
RUN npm ci --no-audit --no-fund \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

COPY . ./
RUN npm run build \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid


# nginx 版本＋digest 都釘死(2026-08-08,氣隙凍結前)。1.27 線已於 2025-06-24
# EOL,現行穩定線是 1.30;digest 是 manifest-list 的那一個,與 compose 的
# nginx(infra/compose/platform.yml)同一個 image:
#   docker buildx imagetools inspect nginx:1.30.4-alpine
# ⚠ 換版本時 tag 與 digest 要一起換 —— 不一致時實際拉的是 digest。
FROM nginx:1.30.4-alpine@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46 AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
