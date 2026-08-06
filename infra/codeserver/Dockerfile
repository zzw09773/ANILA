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
#     (1000:1000)** 的檔案 chown 成 runtime UID/GID。所以擴充**必須以 coder
#     的身分**安裝,fixuid 才認得、才會跟著 remap。
#     (2026-08-06 起改用 `USER root` + `su coder -c`,不是 `USER coder` ——
#      擁有者一樣是 1000:1000,原因見下面第 2 段那個 ⚠。)
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
# 檔案要屬 1000:1000 → runtime 由 fixuid remap。
#
# ⚠ 為什麼是 `USER root` + `su coder -c`,而不是原本的 `USER coder`:
#   這台建置主機的 Symantec DCS 代理會在**容器啟動當下**把 `/var/lib/sdcssagent`
#   與 `/run/sisidsdaemon.pid` 注入進容器的可寫層,而 `/run` 是 root:root 0755
#   —— uid 1000 刪不掉那個 pid 檔。代理在建置容器結束後會自己把 pid 檔刪掉,
#   layer metadata 卻還記著它,於是這一層讓整張映像**再也 `docker save` 不出來**:
#     open …/merged/run/sisidsdaemon.pid: no such file or directory
#   2026-08-06 驗收就是這樣把這張映像擋下來的(同一份 Dockerfile,作者那次建置
#   沒中、驗收那次中了 —— 這不是映像的性質,是每次建置各擲一次骰子)。
#   ⚠ **後面補一層 root RUN 清掉是無效的**,不要退回去:實測(alpine,layer A 被
#   注入不清、layer B 清)攤平後的 rootfs 乾淨、**掃描器判乾淨**,而 `docker save`
#   照樣失敗 —— 早一層 commit 進去的懸空項目修不回來。清理**必須**發生在被注入的
#   那一個 RUN 自己的 commit 之前,所以這個 RUN 得由 root 收尾。
#   `su coder -c` 給的身分與環境跟 `USER coder` 相同(實測 uid=1000 gid=1000、
#   HOME=/home/coder、PWD=/home/coder),擴充檔案的擁有者不變,fixuid 照常認得。
#
# ⚠ 中間那兩個 rm 不是順手打掃,是匯出閘門的硬需求:
#   `.../code-server/logs/<ts>/` 與 `coder-logs/*.log` 是上面三次
#   `--install-extension` / `--list-extensions` **在 build 期**寫下的執行期
#   日誌(2026-08-06 掃描實測:六筆違規全出自這裡,時戳 20260802T1438 正是
#   當時建置的時刻)。runtime 不需要它們**預先存在**:實測開機後 `coder-logs`
#   會被重建,`logs/` 沒有回來,而服務照樣起得來、`/healthz` 回 200。
USER root
RUN su coder -c 'code-server --install-extension ms-azuretools.vscode-docker \
      && code-server --install-extension ms-azuretools.vscode-containers \
      && code-server --list-extensions \
      && rm -rf /home/coder/.config \
      && rm -rf /home/coder/.local/share/code-server/logs \
                /home/coder/.local/share/code-server/coder-logs' \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ── 3. 煙霧測試:binary 進得來、擴充烤得進去(build 失敗早於部署失敗)──
# 這一層順便清掉兩個**不是我們產生**、但會被匯出閘門擋下來的東西:
#
#   (a) `/usr/lib/code-server/node_modules/httpolyglot/test/` —— 上游
#       codercom/code-server 映像自帶相依套件,它的 test fixture 裡有一把
#       **真的 RSA 私鑰**(server.key,測試用自簽對;2026-08-06 掃描器的內容
#       規則抓到的就是它)。httpolyglot 本身是 runtime 相依(同一個 port 同時
#       吃 http/https 靠它),**不能**整包移除;但 package.json 的進入點是
#       lib/index.js,`test/` 只有測試碼與 fixture,拿掉不影響載入。
#       刪整個 `test/` 而不是只刪那把 .key:同一對的 .crt 留著也沒用途,而且
#       只盯檔名的話,上游哪天換個 fixture 名字這條就默默失效了。
#       ⚠ 誠實話:這是**上游 base layer** 的檔案,我們只能在自己的層裡遮掉。
#         攤平的 rootfs(真正跑起來的東西、掃描器看的東西)乾淨了,但
#         `docker save` 仍會把 base layer 那份 bytes 搬進內網。要真的移除得
#         換 base image 或壓平映像,不在這一包的範圍。
#   (b) `/var/lib/sdcssagent/` 與 `/run/sisidsdaemon.pid` —— 上面那段講的注入。
#       **每一個 RUN 都要自己收尾**,因為每個 RUN 各是一次容器啟動、各擲一次骰子
#       (2026-08-06 觀測到的注入命中率見交付報告,遠不是罕見事件)。
#       路徑不存在時 `rm -rf` 回 0,沒有這個代理的建置主機上整段是 no-op。
RUN docker --version \
 && test -d /home/coder/.local/share/code-server/extensions \
 && rm -rf /usr/lib/code-server/node_modules/httpolyglot/test \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

USER 1000
