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

# ── build 期產物清理:為什麼每個 RUN 尾巴都掛一段 rm ─────────────────────────
# 兩個來源,兩個都在 build 期產生、都會被 `docker save` 帶進內網:
#
#   1. `/root/.npm/_logs/*-debug-0.log` —— npm 每跑一次就寫一份 debug log,
#      `npm ci` 結束後它還留在那裡(2026-08-06 掃描實測就是這一條把
#      pptx-renderer 弄成紅燈)。
#   2. `/var/lib/sdcssagent/` 與 `/run/sisidsdaemon.pid` —— **開發機**的
#      Symantec DCS 防護代理(sisidsdaemon)會在容器啟動時把自己的執行期目錄
#      與 pid 檔注入進容器的可寫層;目錄裡有 IDS 日誌與這台主機的 agent 設定。
#      2026-08-06 實測:一個只 `sleep 20` 的 alpine build 照樣被注入,所以
#      **任何**在這台機器上建的映像都會中,跟 Dockerfile 寫什麼無關。同一次
#      實測也確認注入只發生在容器啟動當下,rm 掉之後 40–100 秒都沒被補回來
#      —— 所以 RUN 尾端刪是有效的,不是跟背景程序賽跑。
#      ⚠ pid 檔那條不是為了掃描器(掃描器不看它),是為了 `docker save`:
#      代理會在建置容器結束後把 pid 檔刪掉,layer 的 metadata 卻還記著它,
#      於是匯出時炸 `open …/merged/run/sisidsdaemon.pid: no such file or
#      directory`(build-and-export 的 REBUILD_ON_SAVE_FAIL 就是為這件事存在
#      的)。2026-08-06 實測:同一個 RUN 裡 rm 掉之後 `docker save` 就過了。
#
# ⚠ 為什麼刪在**同一個 RUN**而不是最後補一個 `RUN rm`:掃描器看的是
#   `docker export` 的攤平檔案系統,補一層 rm 也會過;但 `docker save`
#   (內網匯出真正用的那個)搬的是**每一層**,早一層的 bytes 還在裡面。
#   刪在同一個 RUN 才是真的沒有進去過。
# ⚠ 路徑不存在時 `rm -rf` 回 0,所以沒有這個代理的建置主機上這段是 no-op。

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
 && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app

# Copy vendored deps + the rest of the skill. 
COPY package.json package-lock.json ./
RUN npm ci --omit=dev \
 && rm -rf /root/.npm/_logs /var/lib/sdcssagent /run/sisidsdaemon.pid
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
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "server.js"]
