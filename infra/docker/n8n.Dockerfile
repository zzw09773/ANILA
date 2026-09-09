# Rebase official n8n 2.38.1 onto current Alpine 3.24 so apk can apply
# OS fixes. The upstream image is a Docker Hardened Image without apk.
FROM n8nio/n8n:2.38.1@sha256:9f21fbf422982bbdddc31085c180bef82d59cc608ba16dfec4fc48611d0b51b8 AS upstream
FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS patches
RUN apk add --no-cache npm  && mkdir -p /tmp/n8n-cve  && cd /tmp/n8n-cve  && npm pack multer@2.3.0 @xmldom/xmldom@0.8.15 js-yaml@4.3.2 @tiptap/core@3.30.5 nodemailer@9.1.0 toml@4.2.0

FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
USER root
RUN apk upgrade --no-cache  && apk add --no-cache libstdc++ git openssl openssh-client tini  && adduser -D -u 1000 node  && mkdir -p /home/node  && chown node:node /home/node  && rm -rf /var/cache/apk/*
COPY --from=upstream /usr/bin/node /usr/bin/node
COPY --from=upstream /usr/local /usr/local
COPY --from=upstream /docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=patches /tmp/n8n-cve /tmp/n8n-cve
RUN set -eu;     PNPM=/usr/local/lib/node_modules/n8n/node_modules/.pnpm;     tar -xzf /tmp/n8n-cve/multer-2.3.0.tgz -C "$PNPM/multer@2.2.0/node_modules/multer" --strip-components=1;     tar -xzf /tmp/n8n-cve/xmldom-xmldom-0.8.15.tgz -C "$PNPM/@xmldom+xmldom@0.8.14/node_modules/@xmldom/xmldom" --strip-components=1;     tar -xzf /tmp/n8n-cve/js-yaml-4.3.2.tgz -C "$PNPM/js-yaml@4.3.1/node_modules/js-yaml" --strip-components=1;     tar -xzf /tmp/n8n-cve/tiptap-core-3.30.5.tgz -C "$PNPM/@tiptap+core@3.27.0_@tiptap+pm@3.27.0/node_modules/@tiptap/core" --strip-components=1;     tar -xzf /tmp/n8n-cve/nodemailer-9.1.0.tgz -C "$PNPM/nodemailer@8.0.10/node_modules/nodemailer" --strip-components=1;     tar -xzf /tmp/n8n-cve/toml-4.2.0.tgz -C "$PNPM/toml@3.0.0/node_modules/toml" --strip-components=1;     rm -rf /tmp/n8n-cve
USER node
WORKDIR /home/node
EXPOSE 5678
ENTRYPOINT ["tini", "--", "/docker-entrypoint.sh"]
