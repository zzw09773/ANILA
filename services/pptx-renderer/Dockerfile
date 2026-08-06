# pptx-renderer image — Node + LibreOffice + Poppler, all in one container.
#
# Why bookworm-slim and not alpine:
#   - LibreOffice on alpine is non-trivial (the official packages haven't
#     been kept current for headless conversion). bookworm has libreoffice-
#     core in apt with the headless converter we need.
#   - Final image is ~600 MB; sounds big but that's almost entirely the
#     LibreOffice runtime + Java JRE it pulls in. The Node runtime + our
#     vendored node_modules are <200 MB combined.
#
# Why we COPY node_modules instead of `npm install` at build time:
#   - The whole reason we vendored deps in the first place (option A) is
#     so this image works in air-gapped builds. Re-running npm install
#     here would defeat that.
#   - Dependencies are already pinned by what's on disk; stale lockfile
#     vs container drift can't happen.

FROM node:22-bookworm-slim

# LibreOffice for the .pptx → PDF conversion in /screenshots, poppler-utils
# for the PDF → PNG step. fonts-noto-cjk gives us decent CJK glyph coverage
# in the rendered PDFs (otherwise Chinese text comes out as ???? boxes).
# tini = proper PID-1 reaper so node child processes don't zombie.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libreoffice-core \
        libreoffice-impress \
        poppler-utils \
        fonts-noto-cjk \
        fonts-noto-cjk-extra \
        tini \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy vendored deps + the rest of the skill. 
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
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
    NODE_ENV=production

RUN mkdir -p $PPTX_TMP_DIR

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
RUN chown node:node $PPTX_TMP_DIR
USER node

EXPOSE 7100

# tini ensures SIGTERM propagates from compose down → node → soffice
# children cleanly; without it a hanging soffice can leave zombie procs
# that wedge the container restart.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "server.js"]
