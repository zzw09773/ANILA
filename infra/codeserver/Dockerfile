# syntax=docker/dockerfile:1
# ── ANILA code-server ────────────────────────────────────────────────────
# 內網維護用的瀏覽器 IDE:掛整個 ANILA repo,並讓 VS Code 的 Docker 擴充
# 能透過 /var/run/docker.sock 管平台自己的容器。
#
# ⚠ air-gap 前提:平台主機 (10.53.100.15) runtime 抓不到任何東西。所以
#   「要下載的」一律在**開發機 build 期**做完烤進 image,內網只負責跑:
#     1. docker CLI  — 從官方 docker:<ver>-cli 直接 COPY 靜態 binary
#                      (不加 apt repo、不裝 engine,只要 client 就夠)。
#     2. Docker 擴充 — build 期用 code-server --install-extension 從
#                      open-vsx 拉下來烤進 image;內網開機即有。
#   image 產出後用 docker save/load 或內部 registry 送進內網,不要在
#   內網主機上 `--build`(那會需要外網)。
#
# 權限模型(踩過的坑,別回退):
#   - 上游 entrypoint 跑 `fixuid`,會把 /home/coder 底下**原本屬於 coder
#     (1000:1000)** 的檔案 chown 成 runtime UID/GID。所以擴充要以
#     `USER coder` 安裝,fixuid 才認得、才會跟著 remap。
#   - fixuid **不碰 root 擁有的檔案**。`/home/coder/.config` 是 named
#     volume,Docker 建 mountpoint 時是 root:root → fixuid 跳過 → 舊版
#     code-server 開機就 `EACCES: mkdir '/home/coder/.config/code-server'`
#     無限重啟。修法是 compose 的 `codeserver-init` one-shot service 先
#     把 volume chown 成 runtime UID/GID,不是在這裡 chmod 777。
#   - 因此本檔**故意**刪掉 build 期產生的 /home/coder/.config:留著會讓
#     Docker 用 image 內容 seed 空 volume,行為隨 volume 是否為空而變,
#     不好推理。統一交給 init service 處理。

ARG CODE_SERVER_VERSION=4.127.0
ARG DOCKER_CLI_VERSION=28-cli

FROM docker:${DOCKER_CLI_VERSION} AS dockercli

FROM codercom/code-server:${CODE_SERVER_VERSION}

# ── 1. docker CLI(靜態 binary,build 期烤入)────────────────────────────
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# ── 2. Docker 擴充(build 期從 open-vsx 取得)───────────────────────────
# ms-azuretools.vscode-docker v2 之後只是薄殼,真正管容器/映像檔的 UI 在
# ms-azuretools.vscode-containers。兩個都裝,內網才不會點開是空的。
# 以 coder 身分安裝 → 檔案屬 1000:1000 → runtime 由 fixuid remap。
USER coder
RUN code-server --install-extension ms-azuretools.vscode-docker \
 && code-server --install-extension ms-azuretools.vscode-containers \
 && code-server --list-extensions \
 && rm -rf /home/coder/.config

# ── 3. 煙霧測試:binary 進得來、擴充烤得進去(build 失敗早於部署失敗)──
USER root
RUN docker --version \
 && test -d /home/coder/.local/share/code-server/extensions

USER 1000
