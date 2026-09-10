# pptx-renderer image — Node + LibreOffice + Poppler, all in one container.
#
# Debian bookworm LibreOffice cannot be Trivy-cleared (nofix glib/perl/xml/zlib).
# Alpine 3.24 ships libreoffice-impress + poppler-utils; switch the whole
# runtime there. Node stays official node:22-alpine so sharp musllinux wheels
# resolve without a compiler.
#
# Why we COPY lockfile + npm ci instead of `npm install` at build time:
#   - Dependencies are already pinned by what is on disk; stale lockfile
#     vs container drift cannot happen. Re-running unpinned npm install
#     would defeat the air-gap freeze.
#
# Build-layer DCS cleanup; rationale and same-RUN rule: services/asr-decoder/Dockerfile.

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32

# LibreOffice for .pptx → PDF in /screenshots, poppler-utils for PDF → PNG.
# font-noto-cjk covers CJK glyphs (otherwise Chinese text becomes boxes).
# tini = PID-1 reaper so soffice children do not zombie.
RUN apk add --no-cache \
        libreoffice \
        libreoffice-impress \
        openjdk21-jre \
        poppler-utils \
        font-noto-cjk \
        tini \
        libstdc++ \
 && apk upgrade --no-cache libuuid libcrypto3 libssl3 \
 && rm -rf /var/cache/apk/* /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app

# Copy vendored deps + the rest of the skill. 
COPY package.json package-lock.json ./
COPY vendor ./vendor
COPY scripts/patch-image-size-cve.js ./scripts/patch-image-size-cve.js
RUN npm ci --omit=dev \
&& node scripts/patch-image-size-cve.js \
&& rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /root/.npm /var/lib/sdcssagent /run/sisidsdaemon.pid
COPY server.js ./
# icons.js is the Phase 3 concept→Heroicons resolver. Required by
# server.js at startup; missing it manifests as MODULE_NOT_FOUND.
COPY icons.js ./
# Optional: copy the rest of the skill files for runtime introspection
# (SKILL.md / pptxgenjs.md / scripts/) so an operator can attach into
# the container and run helper scripts. They don't affect /render or
# /screenshots paths.
COPY *.md ./
COPY scripts ./scripts

ENV PORT=7100 \
    PPTX_TMP_DIR=/var/anila/pptx-out \
    NODE_ENV=production \
    SAL_USE_VCLPLUGIN=svp \
    HOME=/home/node

RUN mkdir -p $PPTX_TMP_DIR \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ── 降權:非 root 跑 (FAKE-CONTROLS #50) ────────────────────────────────────
# 這支不用自己建帳號:node 官方映像已經帶一個 **釘死的** `node` = 1000:1000
# (getent passwd node → node:x:1000:1000::/home/node),而且 /home/node 存在
# 且屬於它 —— soffice 會在 $HOME 下建 UserInstallation,HOME 不可寫的話
# /screenshots 會在轉 PDF 那一步炸掉。
# 用 1000:1000 而不是跟 csp 一樣的 10001:10001 是刻意的:這個服務**沒有任何
# bind mount**,不跟誰共用檔案,沒有對齊的必要。
#
# $PPTX_TMP_DIR 是唯一要寫的路徑:server.js:442 在載入時 mkdir(recursive),
# 目錄已存在時那行是 no-op 而不會報錯 —— 真正會炸的是後面 :1418 的
# writeFile / :1469 的 writeFileSync。所以這裡一定要 chown,不能只靠 mkdir。
# (:1478 的 mkdtemp 走 os.tmpdir() = /tmp,world-writable+sticky,不用處理。)
RUN chown node:node $PPTX_TMP_DIR \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER node

EXPOSE 7100

# tini ensures SIGTERM propagates from compose down → node → soffice
# children cleanly; without it a hanging soffice can leave zombie procs
# that wedge the container restart.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["node", "server.js"]
