# Platform nginx with Alpine libuuid upgraded past Trivy CVE-2026-53612.
FROM nginx:stable-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c
USER root
RUN apk update && apk upgrade --no-cache libuuid \
    && rm -rf /var/cache/apk/* \
    && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
