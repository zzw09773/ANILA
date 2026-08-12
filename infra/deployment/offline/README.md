# 平台 Python wheelhouse（氣隙補丁）

這一包的承諾很窄：在有網路的建置端，把平台六個 Python 服務的相依閉包收成兩套 ABI wheelhouse，讓已出貨映像可以在氣隙內做 Python 依賴的 overlay patch。它不是從零離線重建方案；Dockerfile 在 pip 之前仍有 apt 等建置步驟，這一階不處理那條路。

## 內容與收集

`build-platform-wheelhouse.sh` 從自身位置推導 repo root，所以可從任何 worktree 位置執行。它只接受 `all`、`cp313`、`cp312` 三種目標，預設收集兩套：

- `cp313`：csp、ingestion-worker、router、anila-studio、asr-gateway。
- `cp312`：asr-decoder。

csp 只收 `services/csp/requirements.txt` 加上 local `anila-core[rag]`；`requirements-dev.txt` 是 test-only，明確排除。ingestion-worker 收自身 pyproject 加 `anila-core[rag]`，router 收 local `anila-core`（無 extras）與自身 requirements，anila-studio、asr-gateway、asr-decoder 各只收自己的 pyproject；asr-gateway 不收 `anila-core` closure。

每套收集都在版本相符的容器內完成：cp313 用 `docker run ... python:3.13-slim`，cp312 用 `docker run ... python:3.12-slim`。工具不使用 `pip --platform` 或 `pip --python-version` 交叉標記；sdist-only 相依會在同一個對應容器內由 `pip wheel` 編成 wheel。

各服務會依序 resolve；每次 resolve 先把該次實際收集到的 wheels 放進自己的暫存目錄，從該目錄產生 manifest，再把 wheels append 到同一個 ABI house。manifest 不是另一次 re-resolve 的結果，而是該 service resolve 真正收集到的 `Name==Version` 集合；輸出會排序且每個 distribution 在單一 manifest 只出現一次。

```text
infra/deployment/offline/dist/cp313/
  csp.freeze.txt
  ingestion-worker.freeze.txt
  router.freeze.txt
  anila-studio.freeze.txt
  asr-gateway.freeze.txt
infra/deployment/offline/dist/cp312/
  asr-decoder.freeze.txt
```

`dist/` 已加入 `.gitignore`；house 與 manifest 是交付產物，不進 PUBLIC repo。router 的 `pydantic-settings` 也已移到 `services/anila-core-router/requirements.txt`，Dockerfile 與 collector 共用同一份宣告。

同一 ABI house 允許同一 distribution 出現多個版本；這是目前已出貨映像的真實狀態，不是 audit 錯誤。cp313 已量到 csp 使用較舊版本，而 ingestion-worker、asr-gateway 使用較新版本；平台層版本漂移列在 `docs/OWNER-QUESTIONS.md` Q52，本 wheelhouse 工具鏈不替平台做版本收斂。

audit 的預設 CLI 是 `python3 audit-wheelhouse.py <house>`，會遞迴尋找該 house 下的 `*.freeze.txt`。它保留空 house 與 `*.tar.gz` 硬閘，並檢查 manifest→wheel、wheel→manifest 的雙向閉包，以及單一 manifest 內每個 distribution 只出現一次；不再以 house-wide 的多版本作為失敗條件。

local `anila-core` 會先打成 wheel，並以 PEP 508 `file://` constraint 鎖在 repo 內來源。`--find-links` 只是增加候選來源，單獨使用仍可能讓公開 index 的同名套件被選走；`file://` direct-reference constraint 是這裡的 dependency-confusion guard。

## 氣隙內 overlay patch

補丁是以既有已出貨映像為基底，只重跑 pip 這一層。先複製該服務的 freeze manifest，再把目標套件的行改成新版本；例如本例的行必須由 `pydantic-settings==2.14.2` 改成 `pydantic-settings==2.15.0`。`-c` 必須指向這份已編輯的副本，不能指向仍記錄舊版本的原始 manifest。BuildKit 的 bind mount 只在該 `RUN` 存在，wheel 不會進 image layer；最後把已編輯的 manifest 保存到 patched image，作為新的 freeze record：

```dockerfile
# syntax=docker/dockerfile:1.7
FROM <shipped-image>
RUN --mount=type=bind,source=infra/deployment/offline/dist/cp313,target=/wheelhouse,readonly \
    cp /wheelhouse/csp.freeze.txt /tmp/csp.freeze.txt && \
    sed -i -E 's/^pydantic-settings==2\.14\.2$/pydantic-settings==2.15.0/' \
      /tmp/csp.freeze.txt && \
    pip install --no-index --find-links /wheelhouse \
        -c /tmp/csp.freeze.txt 'pydantic-settings==2.15.0' && \
    mkdir -p /opt/anila/freeze && \
    cp /tmp/csp.freeze.txt /opt/anila/freeze/csp.freeze.txt
```

以下命令是同一 copy→edit→install 流程的 cp313/csp 離線演練；可在乾淨的 `python:3.13-slim` 中直接執行：

```bash
REPO_ROOT="$(pwd -P)"
docker run --rm --network=none \
  --mount "type=bind,src=$REPO_ROOT/infra/deployment/offline/dist/cp313,dst=/wheelhouse,readonly" \
  python:3.13-slim sh -eu -c '
    cp /wheelhouse/csp.freeze.txt /tmp/csp.freeze.txt
    grep -qx "pydantic-settings==2.14.2" /tmp/csp.freeze.txt
    sed -i -E "s/^pydantic-settings==2\.14\.2$/pydantic-settings==2.15.0/" /tmp/csp.freeze.txt
    grep -qx "pydantic-settings==2.15.0" /tmp/csp.freeze.txt
    pip install --no-index --find-links /wheelhouse \
      -c /tmp/csp.freeze.txt "pydantic-settings==2.15.0"
    pip check
  '
```

`cp312` patch 對應 `dist/cp312`；asr-decoder 的實際演練要以已出貨的 CUDA 基底映像進行。wheel **絕對不可 COPY 進任何 image layer**：即使後面刪掉，歷史 layer 仍會讓 `docker save` 變大，也會觸發 `infra/deployment/scripts/scan-image-artifacts.sh` 的交付檢查。補丁 Dockerfile 應只使用上面的 BuildKit `RUN --mount=type=bind` 掛入 wheelhouse；只保存 manifest，不保存 wheel。

主機上的 patch constraint 必須來自 `dist/<abi>/<service>.freeze.txt` 的已編輯副本；在上面的 bind mount 內，原始檔是 `/wheelhouse/<service>.freeze.txt`，副本才是 pip 的 `-c` 來源。`<service>` 必須與 shipped image 一致。若省略 `-c`，pip 會把同一 house 內另一服務的版本當成候選，並可能悄悄升級該服務原本使用的依賴；manifest 就是 patch-time constraints layer。

這裡是 patch 的依賴安裝能力，不會替已出貨映像重新跑 apt，也不會把 wheelhouse 自動接進線上 compose 或 intranet export 建置行為。

## 明確不包含

- apt / npm / Playwright Chromium / 字型下載與其他非 Python build-time 網路依賴。
- 從零開始的 offline Docker rebuild；這一階的 apt mirror 等外部條件尚未定義。
- `infra/models` 映像；是否納入待 `docs/OWNER-QUESTIONS.md` 的 Q51 裁定。

## 離線 audit 測試

audit 工具只使用 Python 標準函式庫，測試不下載套件、不連網；測試覆蓋 clean pass、two versions with covering manifests pass、empty-house fail、manifest entry without wheel fail、orphan wheel fail、duplicate distribution in one manifest fail、tarball fail。除 empty-house 外，所有 failure fixture 都是非空 house：

```bash
python3 -m unittest discover -s infra/deployment/offline/tests -p 'test_*.py' -v
```

## Commander acceptance（本次實作後執行）

以下命令是完整 acceptance 清單；收集與 `--network=none` 驗證需要 Docker 及相符的 Python image，overlay patch drill 還需要已交付的 shipped image。

### 1. 收集兩套 ABI

在 repo root 執行完整收集；這一步需要可連網的 Docker：

```bash
bash infra/deployment/offline/build-platform-wheelhouse.sh
```

成功條件是兩個 house 都完成 audit，cp313 產生五個 service manifests，cp312 產生 `asr-decoder.freeze.txt`；不應再產生 ABI 根目錄的單一 freeze manifest。接著可直接檢查：

```bash
python3 infra/deployment/offline/audit-wheelhouse.py infra/deployment/offline/dist/cp313
python3 infra/deployment/offline/audit-wheelhouse.py infra/deployment/offline/dist/cp312
find infra/deployment/offline/dist/cp313 infra/deployment/offline/dist/cp312 \
  -maxdepth 1 -type f -name '*.freeze.txt' -print -exec sed -n '1,3p' {} \;
```

### 2. 在乾淨、版本相符容器按 service manifest 做安裝驗證

同一 house 可能有兩個版本的同一 distribution，因此不能把整個 house 的所有 wheels 混裝進一個 venv。以下每個 service 都建立新的 venv，並同時以 manifest 作為 requirements 與 constraints；命令不使用網路：

```bash
REPO_ROOT="$(pwd -P)"
docker run --rm --network=none \
  --mount "type=bind,src=$REPO_ROOT/infra/deployment/offline/dist/cp313,dst=/wheelhouse,readonly" \
  python:3.13-slim sh -eu -c '
    for service in csp ingestion-worker router anila-studio asr-gateway; do
      python -m venv "/tmp/verify-$service"
      "/tmp/verify-$service/bin/python" -m pip install --no-index --find-links /wheelhouse \
        -c "/wheelhouse/$service.freeze.txt" \
        -r "/wheelhouse/$service.freeze.txt"
      "/tmp/verify-$service/bin/python" -m pip check
    done
  '

docker run --rm --network=none \
  --mount "type=bind,src=$REPO_ROOT/infra/deployment/offline/dist/cp312,dst=/wheelhouse,readonly" \
  python:3.12-slim sh -eu -c '
    python -m venv /tmp/verify-asr-decoder
    /tmp/verify-asr-decoder/bin/python -m pip install --no-index --find-links /wheelhouse \
      -c /wheelhouse/asr-decoder.freeze.txt \
      -r /wheelhouse/asr-decoder.freeze.txt
    /tmp/verify-asr-decoder/bin/python -m pip check
  '
```

### 3. 兩套 ABI 各做一次 `--network=none` overlay patch drill

先在氣隙主機準備已交付、已 `docker load` 的 shipped image，並把本次要升級的依賴、舊版本、新版本與輸出 tag 填入變數；base image 必須已在本機，不能靠 build 時拉取。建置中的 `RUN` 必須先複製並更新 service manifest，再用更新後的副本安裝，並把該副本保存為 image 內的新 freeze record：

```bash
export SHIPPED_IMAGE_CP313='<shipped-cp313-image>'
export PATCH_PACKAGE_CP313='<package-name>'
export PATCH_OLD_VERSION_CP313='<old-version>'
export PATCH_NEW_VERSION_CP313='<new-version>'
export PATCH_SERVICE_CP313='<csp|ingestion-worker|router|anila-studio|asr-gateway>'
export PATCH_IMAGE_CP313='anila-wheelhouse-patch:cp313'
export SHIPPED_IMAGE_CP312='<shipped-cp312-asr-decoder-image>'
export PATCH_PACKAGE_CP312='<package-name>'
export PATCH_OLD_VERSION_CP312='<old-version>'
export PATCH_NEW_VERSION_CP312='<new-version>'
export PATCH_SERVICE_CP312='asr-decoder'
export PATCH_IMAGE_CP312='anila-wheelhouse-patch:cp312'
REPO_ROOT="$(pwd -P)"

docker build --network=none --build-arg SERVICE_NAME="$PATCH_SERVICE_CP313" \
  --build-arg PACKAGE_NAME="$PATCH_PACKAGE_CP313" \
  --build-arg OLD_VERSION="$PATCH_OLD_VERSION_CP313" \
  --build-arg NEW_VERSION="$PATCH_NEW_VERSION_CP313" \
  -t "$PATCH_IMAGE_CP313" -f - "$REPO_ROOT" <<EOF
# syntax=docker/dockerfile:1.7
FROM $SHIPPED_IMAGE_CP313
ARG SERVICE_NAME
ARG PACKAGE_NAME
ARG OLD_VERSION
ARG NEW_VERSION
RUN --mount=type=bind,source=infra/deployment/offline/dist/cp313,target=/wheelhouse,readonly \
    cp /wheelhouse/\${SERVICE_NAME}.freeze.txt /tmp/\${SERVICE_NAME}.freeze.txt && \
    test "\$(grep "^\${PACKAGE_NAME}==" /tmp/\${SERVICE_NAME}.freeze.txt)" = "\${PACKAGE_NAME}==\${OLD_VERSION}" && \
    sed -i -E "s|^\${PACKAGE_NAME}==\${OLD_VERSION}\$|\${PACKAGE_NAME}==\${NEW_VERSION}|" /tmp/\${SERVICE_NAME}.freeze.txt && \
    pip install --no-index --find-links /wheelhouse \
      -c /tmp/\${SERVICE_NAME}.freeze.txt "\${PACKAGE_NAME}==\${NEW_VERSION}" && \
    mkdir -p /opt/anila/freeze && \
    cp /tmp/\${SERVICE_NAME}.freeze.txt /opt/anila/freeze/\${SERVICE_NAME}.freeze.txt
EOF

docker build --network=none --build-arg SERVICE_NAME="$PATCH_SERVICE_CP312" \
  --build-arg PACKAGE_NAME="$PATCH_PACKAGE_CP312" \
  --build-arg OLD_VERSION="$PATCH_OLD_VERSION_CP312" \
  --build-arg NEW_VERSION="$PATCH_NEW_VERSION_CP312" \
  -t "$PATCH_IMAGE_CP312" -f - "$REPO_ROOT" <<EOF
# syntax=docker/dockerfile:1.7
FROM $SHIPPED_IMAGE_CP312
ARG SERVICE_NAME
ARG PACKAGE_NAME
ARG OLD_VERSION
ARG NEW_VERSION
RUN --mount=type=bind,source=infra/deployment/offline/dist/cp312,target=/wheelhouse,readonly \
    cp /wheelhouse/\${SERVICE_NAME}.freeze.txt /tmp/\${SERVICE_NAME}.freeze.txt && \
    test "\$(grep "^\${PACKAGE_NAME}==" /tmp/\${SERVICE_NAME}.freeze.txt)" = "\${PACKAGE_NAME}==\${OLD_VERSION}" && \
    sed -i -E "s|^\${PACKAGE_NAME}==\${OLD_VERSION}\$|\${PACKAGE_NAME}==\${NEW_VERSION}|" /tmp/\${SERVICE_NAME}.freeze.txt && \
    pip install --no-index --find-links /wheelhouse \
      -c /tmp/\${SERVICE_NAME}.freeze.txt "\${PACKAGE_NAME}==\${NEW_VERSION}" && \
    mkdir -p /opt/anila/freeze && \
    cp /tmp/\${SERVICE_NAME}.freeze.txt /opt/anila/freeze/\${SERVICE_NAME}.freeze.txt
EOF
```

對兩個 rebuilt tag 都要以該服務平常的 container command 啟動並跑 health/import smoke；至少確認 patch dependency 的版本已變更、服務能起來，且啟動時使用 `--network=none`。例如 router 可在 container 內執行：

```bash
docker run --rm --network=none "$PATCH_IMAGE_CP313" python -c "import importlib.metadata as m; print(m.version('pydantic-settings'))"
docker run --rm --network=none "$PATCH_IMAGE_CP312" python -c "import importlib.metadata as m; print(m.version('faster-whisper'))"
```

### 4. 任何 rebuilt image 都要過兩道交付閘門

只要 patch drill 或其他原因重建了 image，對每一張 rebuilt tag 都執行掃描與 `docker save`；兩道都要成功：

```bash
bash infra/deployment/scripts/scan-image-artifacts.sh \
  "$PATCH_IMAGE_CP313" "$PATCH_IMAGE_CP312"
docker save "$PATCH_IMAGE_CP313" -o /tmp/anila-wheelhouse-patch-cp313.tar
docker save "$PATCH_IMAGE_CP312" -o /tmp/anila-wheelhouse-patch-cp312.tar
```

不要以 build 成功取代這兩道檢查；尤其 wheel 不得在掃描器或 `docker save` 看到的 image layer 內。
