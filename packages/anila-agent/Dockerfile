# syntax=docker/dockerfile:1
#
# anila-agent — 開發「環境」image（給 MLSteam / air-gap 內網用）。
#
# 這顆只裝「環境」：anila-agent 套件 + 全部依賴 + JupyterLab，**不包源碼、不包 configs**。
# 同事在 MLSteam 的 workspace 自己 clone repo（源碼 + configs 在 workspace，Lab 看得到），
# 用這個 image 預裝好的 Python 環境開發，不必在內網對付 pip。
#
# 在「有網路的外部環境」build，再 `docker save | gzip` 搬進內網 `docker load`。
# 詳見 DOCKER.md / scripts/docker-ship.sh。

# ============================================================
# builder：把套件 + 依賴裝進獨立 venv（編譯工具只留這層）
# ============================================================
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
# 只 COPY 安裝套件所需（pyproject + 套件原始碼）。這份原始碼只用來「裝進 venv」，
# 不會出現在 runtime image 的檔案樹（runtime 只 COPY /opt/venv）。
COPY pyproject.toml README.md ./
COPY anila_agent ./anila_agent

# serving=FastAPI wrapper；pgvector=ANILA 原生檢索(asyncpg)；jupyter=MLSteam Lab 需要；
# dev=pytest/ruff/mypy（完整開發環境）。CSP HTTP retriever 只需 httpx（base deps 已有）。
RUN pip install ".[serving,pgvector,jupyter,dev]"

# build-time 煙霧測試：import 不過就讓 build 當場失敗（外網好除錯）。
RUN python -c "from anila_agent.serving.service_wrapper import app; print('import OK')"

# ============================================================
# runtime：精簡層、非 root；只有「環境」，沒有源碼
# ============================================================
FROM python:3.12-slim AS runtime

# 開發環境必備：git（clone repo）+ 編輯器（nano/vim）+ ca-certificates（https git）
# + less/curl（順手）。slim 這些都沒有。
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates nano vim less curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 執行；建 home 給 jupyter 寫設定/runtime
RUN useradd --create-home --uid 10001 anila

COPY --from=builder /opt/venv /opt/venv

# MLSteam 的 Lab 可能以絕對路徑（/usr/local/bin/jupyter）或靠 PATH 啟動 JupyterLab。
# 把 venv 的 jupyter 連到標準位置，兩種叫法都找得到（避免 ERRORCODE=127）。
RUN ln -sf /opt/venv/bin/jupyter /usr/local/bin/jupyter \
    && ln -sf /opt/venv/bin/jupyter-lab /usr/local/bin/jupyter-lab \
    && ln -sf /opt/venv/bin/jupyter-notebook /usr/local/bin/jupyter-notebook

# 關鍵：JupyterLab terminal 開的是 login bash，會 source /etc/profile 並 reset PATH，
# 把 Docker ENV 的 /opt/venv/bin 弄丟 → terminal 裡 python/anila/pytest 不是預裝環境的。
# 用 /etc/profile.d（login shell）+ /etc/bash.bashrc（互動 bash）讓 venv 永遠在 PATH，
# 並把使用者 shell 設成 bash（比 sh 好用）。
RUN printf 'export VIRTUAL_ENV=/opt/venv\nexport PATH="/opt/venv/bin:$PATH"\n' > /etc/profile.d/venv.sh \
    && chmod 0644 /etc/profile.d/venv.sh \
    && cat /etc/profile.d/venv.sh >> /etc/bash.bashrc \
    && usermod -s /bin/bash anila

# JupyterLab：① server 允許 serve 隱藏檔（.env 等），② file browser 預設「顯示隱藏檔」。
# 否則 .env 在 Jupyter 檔案瀏覽器裡看不到、也編不了。
RUN mkdir -p /etc/jupyter /opt/venv/share/jupyter/lab/settings \
    && printf 'c.ContentsManager.allow_hidden = True\n' > /etc/jupyter/jupyter_server_config.py \
    && printf '{\n  "@jupyterlab/filebrowser-extension:browser": { "showHiddenFiles": true }\n}\n' \
        > /opt/venv/share/jupyter/lab/settings/overrides.json

ENV PATH="/opt/venv/bin:$PATH" \
    SHELL=/bin/bash \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANILA_LOG_LEVEL=INFO

USER anila
WORKDIR /home/anila

EXPOSE 8888
EXPOSE 8200

# 預設啟 JupyterLab（開發環境）。MLSteam 建 Lab 時會帶自己的啟動指令、覆寫這個 CMD。
# 在這顆環境裡跑框架：先 clone repo（有源碼 + configs）進 workspace，再於 repo 內
#   anila                                          # 互動 CLI
#   uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200   # 服務
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser"]
