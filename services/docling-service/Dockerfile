# docling 有 EasyOCR → torch,需要 CUDA 12.6 + cuDNN 9(與 asr-decoder 同世代)。
# ⚠ tag 必須是 `-cudnn-`(cuDNN 9)而不是舊的 `-cudnn8-`,否則模型載入時才
# 炸 libcudnn_ops.so.9 找不到。
# ⚠ 這台是 GPU 主機的服務,不是平台映像。平台映像(docling 的 csp / ingestion-worker
# 那一側)**不帶 docling / torch / easyocr**,只有 httpx client(見
# packages/anila-core/src/anila_core/ingestion/docling_parser.py)。
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS base

# ── build 期產物清理 ────────────────────────────────────────────────────────
# 每個 RUN 尾巴都掛 `rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid`:
# 開發機的 Symantec DCS 代理在容器啟動當下把自己的目錄與 pid 檔注入可寫層
# (含 build 容器),不刪會讓 `docker save` 直接失敗。理由與同一條規則的全文
# 見 services/asr-decoder/Dockerfile。路徑不存在時 rm -rf 回 0,沒這個代理的
# 建置主機上整段是 no-op。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      curl \
      ca-certificates \
      # EasyOCR → cv2 需要 X11/GL 原生函式庫。docling[easyocr] 的閉包裡
      # rapidocr 硬相依**非 headless** 的 opencv-python(會覆寫 headless 版的
      # cv2/),它的 bootstrap 缺 libxcb.so.1 / libGL.so.1 / libglib-2.0.so.0
      # 就 ImportError,而 docling 把它報成「EasyOCR is not installed」——把
      # 除錯的人指去裝套件那條錯路。補原生 lib 是 opencv 在 Debian/Ubuntu
      # 的標準解法;headless 版並存只是多一份磁碟,無害。
      libgl1 \
      libglib2.0-0 \
      libxcb1 \
      libsm6 \
      libxext6 \
 && rm -rf /var/lib/apt/lists/* /var/lib/sdcssagent /run/sisidsdaemon.pid \
 && rm -f /var/log/bootstrap.log

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    # 關掉 docling 的 torch.compile（InferenceSettings.compile_torch_models，
    # env_prefix=DOCLING_）。實測（2026-08-18，本機 CPU、同一份 L312.pdf）：
    # 開 compile 70.5s 且要 C++ 工具鏈（g++/Python.h）才能跑；關掉 6.47s、
    # 完全不需要編譯器。CPU 上 compile 是純負擔，所以烘成關閉。
    # ⚠ 不要翻成 true：本映像不帶編譯器，翻 true 的實測結果是 /health 假 ready
    #   + 每一次 /parse 都 503（InvalidCxxCompiler）。守衛
    #   tests/test_dockerfile_compile_env.py 從本檔讀出這行斷言它維持 false；
    #   若哪天真要在某個 GPU 世代上開 compile，那是新設計（要 reintro 工具鏈 +
    #   重測），不是改這一顆值。
    DOCLING_INFERENCE_COMPILE_TORCH_MODELS=false

RUN python3 -m venv /opt/venv \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

WORKDIR /app

# 依賴宣告 SSOT 是 pyproject.toml;runtime 相依只改那一個檔。裝 `.` 非 `.[dev]`。
COPY pyproject.toml ./
COPY app/ ./app/
# ⚠ docling[easyocr] 拉進 torch + easyocr,是這張映像體積的主要來源(數 GB)。
# 🔴 順序先 torch、後 docling:若「pip install .」在前,docling 要的
#   torch<3.0.0,>=2.2.2 會先從 PyPI 預設 index 拉到 2.13.0(綁 cuda-toolkit 13
#   / nvidia-*-cu13 整家族,數 GB);之後再裝 cu126 只換 torch 本體,**那套 cu13
#   不會被卸掉**,跟 cu12 並存 → 映像暴肥、且內部同時有兩套 CUDA 運行時。2026-08-18
#   實測 build 出 12.7GB 就是這個。反過來先裝 cu126 torch(torchvision 同 index),
#   docling 解析時看到 2.13.0+cu126 已滿足約束,不會再拉 cu13。兩段都要帶
#   --extra-index-url(PyPI 主 index 仍在,cu126 只管 torch/torchvision 這顆
#   local-version wheel;torch 的其餘相依 filelock/sympy/… 仍在 PyPI)。
# 🔴 版本釘死(擁有者 2026-08-18 裁決:釘 cu126)。浮動讓映像內部不一致,且下次
#   重建可能又換。torchvision 大版要跟 torch 對齊(0.28 ↔ 2.13)。升版時「兩邊
#   一起升 + 重跑一次 POST 真的 PDF」,不是只改一個數字。
RUN pip install --upgrade pip \
 && pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu126 \
      torch==2.13.0+cu126 torchvision==0.28.0+cu126 \
 && pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu126 \
      . \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# ⚠ 本站的 CUDA major 由「安裝的那包 torch」決定:`import torch` 會驗
# torch.version.cuda == 你起容器時要對得上的那張卡驅動支援的 major。部署硬前提:
# host 的 `nvidia-smi` 驅動必須支援 CUDA 12.6(≥550.54.15 起支援 12.4+;實際
# 以 nvidia-smi 右上角 "CUDA Version" ≥ 12.6 為準)。驅動不合,症狀是啟動就死,
# 不是慢了。

# 權重刻意不烘進 image:由 compose 以 volume :ro 掛到 DOCLING_ARTIFACTS_DIR。
# 理由同 asr-decoder——air-gap bundle 可獨立替換權重、image 可簽章且體積可控。
RUN mkdir -p /var/anila/docling-artifacts \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid

# Run as non-root for defence-in-depth
RUN useradd --create-home --uid 10001 anila \
 && chown -R anila:anila /app /var/anila/docling-artifacts \
 && rm -rf /var/lib/sdcssagent /run/sisidsdaemon.pid
USER anila

EXPOSE 9100

# 模型在第一次 parse 時載入(EasyOCR / layout model 動輒 GB),ready 之前 /health
# 回 503 → start-period 只要夠 uvicorn 起來即可,冷啟動由 retries 吸收。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=20 \
  CMD curl -sf http://localhost:9100/health > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9100", "--log-level", "info"]
