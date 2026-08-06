# CTranslate2 4.x(faster-whisper 的後端)需要 CUDA 12 + cuDNN 9。
# ⚠ tag 必須是 `-cudnn-`(cuDNN 9)而不是舊的 `-cudnn8-`,否則 model load
# 會在 runtime 才炸 "libcudnn_ops.so.9: cannot open shared object file"。
# cudnn-runtime 這個 variant 同時帶了 cuBLAS 與 cuDNN,兩個 CT2 都要。
# Ubuntu 24.04 內建 python3.12(對齊 pyproject 的 requires-python >=3.11)。
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS base

# ── build 期產物清理:為什麼每個 RUN 尾巴都掛一段 rm ─────────────────────────
# `/var/lib/sdcssagent/` 是**開發機**的 Symantec DCS 防護代理(sisidsdaemon)
# 在容器啟動當下注入進容器可寫層的執行期目錄 —— 裡面是這台主機的 IDS 日誌
# (SISIDSService.log、statistics/statistics.log)與 agent 設定(agent.ini、
# LocalAgent.ini)。2026-08-06 在 anila/asr-decoder:0.1.0 撈到的就是它,檔案
# 時戳(2026-07-30 21:19)剛好落在最後那個 RUN 那一層。實測兩件事:
#   (a) 一個只 `sleep 20` 的 alpine build 照樣被注入 → 跟 Dockerfile 寫什麼
#       無關,**任何**在這台機器上建的映像都會中;
#   (b) 注入只發生在容器啟動當下,rm 之後 40–100 秒都沒被補回來 → RUN 尾端刪
#       是有效的,不是跟背景程序賽跑。
# 同一個代理還會丟一個 `/run/sisidsdaemon.pid`。那條**不是**為了掃描器
# (掃描器不看它),是為了 `docker save`:代理在建置容器結束後把 pid 檔刪掉,
# layer metadata 卻還記著它,匯出時就炸 `open …/merged/run/sisidsdaemon.pid:
# no such file or directory`(build-and-export 的 REBUILD_ON_SAVE_FAIL 正是
# 為這件事存在)。2026-08-06 實測:這張映像原本 `docker save` 就是這樣失敗的,
# 同一個 RUN 裡把 pid 檔一起 rm 掉之後就過了。
# ⚠ 刪在**同一個 RUN**而不是最後補一層 `RUN rm`:掃描器看的是 `docker export`
#   的攤平檔案系統,補一層也會過;但內網匯出用的 `docker save` 搬的是**每一層**,
#   早一層的 bytes 還在裡面。刪在同一個 RUN 才是真的沒進去過。
# ⚠ 路徑不存在時 `rm -rf` 回 0,沒有這個代理的建置主機上整段是 no-op。
#
# `/var/log/bootstrap.log` 是 ubuntu base image 自己帶的 debootstrap 紀錄
# (時戳 2024-10-16,不是我們產生的)。它只能在我們的層裡刪掉 —— bytes 仍留在
# base layer,`docker save` 還是會搬走那一份;攤平後的 rootfs(真正跑起來的
# 東西、掃描器看的東西)是乾淨的,但這一條**不是**「從源頭移除」,是遮蔽。

# - python3 / python3-venv:runtime + PEP 668 隔離(24.04 的系統 python 是
#   externally-managed,不能直接 pip install)
# - curl + ca-certificates:health probe(compose 讀它判 readiness)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid \
 && rm -f /var/log/bootstrap.log

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN python3 -m venv /opt/venv \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app

# 依賴宣告的 SSOT 是 pyproject.toml(對齊 anila-studio)—— 加 runtime 相依
# 只改那一個檔,不會有 Dockerfile drift。裝 `.` 而非 `.[dev]`,pytest 不進
# runtime image。setuptools 要讀得到 package 原始碼才能算 metadata,所以
# app/ 必須在 pip install 之前 COPY。
COPY pyproject.toml ./
COPY app/ ./app/
RUN pip install --upgrade pip && pip install --no-cache-dir . \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ⚠ 權重刻意不烘進 image:
#   1. air-gap bundle 要能獨立替換模型尺寸(medium ↔ large-v3)而不重 build;
#   2. image 保持可簽章、體積可控(large-v3 光權重就 ~3GB);
#   3. 內部版 / 外部版兩種部署掛的是同一份權重目錄,image 完全相同。
# 由 compose 以 volume 掛到 ASR_MODEL_DIR。
RUN mkdir -p /var/anila/asr-models \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app /var/anila/asr-models \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER anila

EXPOSE 9000

# 模型是背景載入的,ready 之前 /health 回 503 → 這裡的 start-period 只要
# 夠 uvicorn 起來即可,權重載入慢由 retries 吸收(large-v3 冷啟動可能要
# 分鐘級,retries × interval 要蓋得住)。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=20 \
  CMD curl -sf http://localhost:9000/health > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--log-level", "info"]
