FROM node:22-alpine AS builder

WORKDIR /app

ARG VITE_CSP_BASE_URL=http://localhost:8000
ARG VITE_ROUTER_BASE_URL=http://localhost:9000

ENV VITE_CSP_BASE_URL=${VITE_CSP_BASE_URL}
ENV VITE_ROUTER_BASE_URL=${VITE_ROUTER_BASE_URL}

COPY package.json package-lock.json* ./
# Use `npm install` (not `npm ci`) so package.json edits pull new deps without
# forcing a host-side lockfile regeneration. Only runs during image build.
RUN npm install --no-audit --no-fund

COPY . ./
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
