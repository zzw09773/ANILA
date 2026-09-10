# syntax=docker/dockerfile:1
# ANILA code-server on node:24-alpine. Do not retag live anila-codeserver:local.
ARG CODE_SERVER_VERSION=4.136.2
FROM docker:29.8.0-cli-alpine3.24@sha256:eccaacfeed644c7de222ff047483568cb988dde95476fbaaf10ea2d04921bb66 AS dockercli
FROM node:24-alpine@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf
ARG CODE_SERVER_VERSION
USER root
RUN apk add --no-cache git openssh-client bash wget tini libstdc++ python3 make g++ krb5-dev linux-headers \
 && apk upgrade --no-cache libuuid libcrypto3 libssl3 \
 && (deluser node || true) && adduser -D -u 1000 -s /bin/bash coder \
 && mkdir -p /home/coder \
 && rm -rf /var/cache/apk/* /var/lib/sdcssagent /run/sisidsdaemon.pid
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN npm install -g --allow-scripts=code-server,argon2 code-server@${CODE_SERVER_VERSION} \
 && apk del --no-cache make g++ python3 krb5-dev linux-headers \
 && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /root/.npm \
 && rm -rf /var/cache/apk/* /var/lib/sdcssagent /run/sisidsdaemon.pid
RUN su coder -c "code-server --install-extension ms-azuretools.vscode-docker && code-server --install-extension ms-azuretools.vscode-containers && code-server --list-extensions && rm -rf /home/coder/.config /home/coder/.local/share/code-server/logs /home/coder/.local/share/code-server/coder-logs" \
 && chmod -R a+rwX /home/coder \
 && rm -rf /usr/local/lib/node_modules/code-server/node_modules/httpolyglot/test /usr/local/lib/node_modules/code-server/lib/vscode/node_modules/httpolyglot/test \
 && rm -f /usr/local/lib/node_modules/code-server/npm-shrinkwrap.json /usr/local/lib/node_modules/code-server/lib/vscode/npm-shrinkwrap.json /usr/local/lib/node_modules/code-server/lib/vscode/extensions/npm-shrinkwrap.json \
 && rm -rf /usr/local/lib/node_modules/code-server/lib/vscode/node_modules/@github /usr/local/lib/node_modules/code-server/lib/vscode/node_modules/@microsoft \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
# Telemetry/MXC packages above are CVE-bearing; keep no-op stubs so workbench can import them.
COPY microsoft-stubs/@microsoft /usr/local/lib/node_modules/code-server/lib/vscode/node_modules/@microsoft
ENV LANG=en_US.UTF-8 ENTRYPOINTD=/entrypoint.d USER=coder HOME=/home/coder
WORKDIR /home/coder
EXPOSE 8080
USER 1000
ENTRYPOINT ["/sbin/tini", "--", "code-server", "--bind-addr", "0.0.0.0:8080"]
CMD ["."]
