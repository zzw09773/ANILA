FROM node:22-alpine AS builder

# Build context = repo root（infra/compose/{platform,dev}.yml 傳入 ../..，
# 同 csp 服務的 root-context 慣例）：anila-shell 以 Vite alias 引用共用設計
# 系統 packages/ui 的「原始碼」（見 vite.config.js），context 必須同時涵蓋
# app 與 packages/ui 兩個目錄，否則 vite build 解析不到 ../../packages/ui。
WORKDIR /app/apps/anila-shell

ARG VITE_CSP_BASE_URL=http://localhost:8000
ARG VITE_ROUTER_BASE_URL=http://localhost:9000
# Serving prefix — 正式部署 '/anila/'(443 同源 subpath,SSO cookie 自然共用);
# 不傳 = '/'(本機 dev 直跑)。
ARG BASE_PATH=/

ENV VITE_CSP_BASE_URL=${VITE_CSP_BASE_URL}
ENV VITE_ROUTER_BASE_URL=${VITE_ROUTER_BASE_URL}
ENV BASE_PATH=${BASE_PATH}

COPY apps/anila-shell/package.json apps/anila-shell/package-lock.json* ./
# Use `npm install` (not `npm ci`) so package.json edits pull new deps without
# forcing a host-side lockfile regeneration. Only runs during image build.
RUN npm install --no-audit --no-fund

# 共用設計系統源碼（app 內相對路徑 ../../packages/ui）。react 由本 app 的
# node_modules 提供（vite resolve.dedupe），套件目錄不需要 npm install。
COPY packages/ui /app/packages/ui
COPY apps/anila-shell ./
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY apps/anila-shell/docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/apps/anila-shell/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
