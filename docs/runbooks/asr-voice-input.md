# 串流語音輸入(ASR)部署 Runbook

> 適用:在既有 ANILA 平台上啟用聊天輸入框旁的麥克風按鈕(邊講邊出字)。
> 兩個新服務、零 DB 變更、零 alembic migration。預設**不啟動** —— 要啟用才
> 開對應 profile。
> 設計與實測證據見 `docs/planning/asr-voice-input-plan.md`;服務內部細節見
> `services/asr-gateway/README.md`、`services/asr-decoder/README.md`。

---

## 0. 先決條件與必讀

- **麥克風 = 兩個必要條件缺一不可**:① 頁面是 secure context(https 或
  localhost —— 用 IP 走 http 測**必然**拿不到麥克風);② nginx 的
  `Permissions-Policy` 已放行 `microphone=(self)`(M4 已改;若被還原,失敗症狀
  與「使用者按拒絕」一模一樣,極難查)。
- **資料門檻不變**:ASR 是新增輸入途徑,不改變任何資料分級結論。機敏以上
  production 仍須依 roadmap §6.1 完成對應 Gate 並經權責人書面核准。
- **兩種部署二選一**(§2、§3)。內部版是內網優先選項(語音不出主機)。
- **全新 air-gapped 主機從零部署 → 直接看 §1.5 的線性 checklist**(開發機打包 →
  打包機 build → 實體搬運 → 目標機載入部署 → 驗收 → 回滾,一路照做)。

---

## 1. 產生共享密鑰

gateway 與 decoder 靠 `ASR_DECODER_TOKEN` 互認,兩邊必須同值。**這是 PUBLIC
repo,密鑰只進 `.env`,絕不進 repo。**

```bash
openssl rand -hex 32          # 產一把,填進兩邊的 .env(見下)
```

---

## 1.5 全新 air-gapped 主機 — 完整線性部署 checklist(內部版)

> 目標情境:一台**全新、完全內網、有 GPU** 的主機(下稱「目標機」),要從零把
> 整個 ANILA 平台 + 語音輸入部署起來。
>
> **ASR 不是獨立部署** —— 它掛在平台上,gateway 要 csp/redis 才能認證。所以
> 語音是**整個平台部署的最後一步**,不是先做的事。
>
> 涉及**三個角色的機器**,別搞混:
>
> | 角色 | 需要外網? | 做什麼 |
> |---|---|---|
> | **開發機** | — | 產源碼快照(`anila-src.tar.gz`)。就是本 repo 這台。 |
> | **打包機** | ✅ 要 | 展開源碼,build image + 下載權重,打成 bundle。 |
> | **目標機 172.16.120.35** | ❌ air-gapped | 只 `docker load` bundle、部署,不 build。 |
>
> 平台本體(csp/db/redis/nginx/TLS/JWT/卡登)的部署細節在
> `docs/runbooks/intranet-zero-to-prod-guide.md` 與 `intranet-deployment-runbook.md`,
> **本文不重複**;下面只把 ASR 相關步驟嵌在正確的位置。

### Part A — 開發機:產源碼快照

- [ ] **A1. 確認工作樹乾淨**(快照才對得回某個 commit,可追溯性的前提)
  ```bash
  cd <repo>
  git status                    # 必須是 "nothing to commit, working tree clean"
  git log -1 --format='%H %s'   # 記下這個 commit —— 這就是本次部署的版本
  ```
- [ ] **A2. 打源碼快照**(免 git,排除會重建/純本機的東西)
  ```bash
  cd <repo 上一層>
  tar czf anila-src.tar.gz \
    --exclude='.git' --exclude='node_modules' --exclude='.venv' \
    --exclude='dist' --exclude='__pycache__' --exclude='*.tar.gz' \
    <repo 目錄名>
  ```
- [ ] **A3. 抽檢**:`.venv`/`.git`/`.env` 沒進去、ASR 的碼有進去
  ```bash
  tar tzf anila-src.tar.gz | grep -c '\.venv/'          # 應為 0
  tar tzf anila-src.tar.gz | grep -c 'services/asr-'    # 應 > 0
  ```
- [ ] **A4. 把 `anila-src.tar.gz` 搬到打包機**(scp / 內部檔案交換,依你們規定)。

### Part B — 打包機(有外網):build + 打 bundle

- [ ] **B1. 展開源碼**
  ```bash
  tar xzf anila-src.tar.gz && cd <repo 目錄名>
  ```
- [ ] **B2. ⚠ 手動先 build decoder image**(**這步不能省**)
  ```bash
  docker build -t asr-decoder:0.1.0 services/asr-decoder
  ```
  理由:model bundle(`04-models.tar.gz`)只 `docker save` **現成的** image、
  **不會幫你 build**。gateway 不用手動 build(它 source=built,平台 bundle 會自動
  build)。CUDA base(`nvidia/cuda:12.6.3-cudnn-runtime`)會烘進 decoder 的 image
  層、`docker save` 一起帶走,**不必單獨處理 base image**。
  (彩排實測:decoder image build 成功、large-v3 fp16 在 GPU 上 90 秒載入、
  ~3.3GB VRAM、真實語音解碼正確。)
- [ ] **B3. 打 bundle**(`WITH_MODELS=1` 才會收 decoder)
  ```bash
  WITH_MODELS=1 bash infra/deployment/intranet/build-and-export-for-intranet.sh <輸出目錄>
  ```
- [ ] **B4. 確認 ASR 兩個 image 都進了 bundle**
  ```bash
  grep asr-decoder <輸出目錄>/MODEL-IMAGE-STATUS.tsv     # 要 "included",不是 "missing"
  grep asr-gateway <輸出目錄>/PLATFORM-IMAGE-LOCK.tsv    # 應有 asr-gateway 一列
  ```
  若 decoder 是 "missing" → B2 沒做或 image tag 不是 `asr-decoder:0.1.0`。
- [ ] **B5. 下載 large-v3 權重**(走**權重通道**,不進 image bundle)
  ```bash
  bash infra/deployment/intranet/download-intranet-models.sh <權重暫存目錄>
  # 產出 <權重暫存目錄>/faster-whisper-large-v3/(內含 model.bin)
  ```

### Part C — 實體搬進內網

- [ ] **C1.** 把這些用你們的實體資料閘道搬到目標機:
  - `<輸出目錄>/*.tar.gz`(所有 image bundle,含 `01-anila-built`、`02-base`、`04-models`…)
  - `<權重暫存目錄>/faster-whisper-large-v3/`(整個目錄)

### Part D — 目標機 172.16.120.35(air-gapped):載入 + 部署

- [ ] **D1. 載入所有 image bundle**
  ```bash
  for f in *.tar.gz; do echo "load $f"; gunzip -c "$f" | docker load; done
  docker images | grep -E 'asr-decoder|asr-gateway'   # 兩個都要在
  ```
- [ ] **D2. 權重就位**:把 `faster-whisper-large-v3/` 放到之後 `ANILA_HF_DIR` 會指到
  的位置(例:`$ANILA_HF_DIR/faster-whisper-large-v3/model.bin` 存在)。
- [ ] **D3. ⚠ 先把整個平台部署起來** —— 照
  `docs/runbooks/intranet-zero-to-prod-guide.md`:TLS 憑證、JWT keypair(缺了
  JWKS 500、登入炸)、卡登 CRL、DB 密碼、csp/db/redis/nginx 全部。
  **這是最大的一塊,ASR 之前必須完成。**
  ```bash
  # 平台起來後,確認 csp 與 redis 健康(ASR 依賴它們)
  docker compose -p anila-platform -f infra/compose/platform.yml ps csp redis
  #   兩個都要 (healthy)
  ```
  ⚠ **順序不可顛倒**:gateway 啟動時會硬性去抓 csp 的 JWKS,連不到就退出
  (fail-closed,與 anila-studio 同款,彩排已證實)。平台的 `depends_on:
  service_healthy` 會擋住,但你手動起 ASR 時務必先確認上面兩個是 healthy。
- [ ] **D4. `.env`(repo root)補 ASR 設定**
  ```bash
  # 產一把共享密鑰(gateway 與 decoder 必須同值)
  openssl rand -hex 32
  ```
  填進 `.env`:
  ```ini
  ASR_DECODER_TOKEN=<上面產的 32-byte hex>
  ASR_GPU=<這台沒有 LLM 在跑的卡號>   # ⚠ 不要跟 LLM 共卡,推論延遲互擾難 debug
  ANILA_HF_DIR=<D2 權重的上層目錄>
  ASR_MODEL_SIZE=large-v3
  ASR_COMPUTE_TYPE=float16            # H100/V100 都用 float16(V100 int8 反而更慢)
  ASR_DECODE_URL=http://asr-decoder:9000   # 內部版預設,走內部 network,免 http 旗標
  # ASR_ALLOW_HTTP_DECODER 維持 0(內部版不需要)
  ```
- [ ] **D5. 起 decoder(GPU)**
  ```bash
  bash infra/deployment/intranet/model-serve.sh up asr-decoder
  # 等它 ready(large-v3 冷啟動分鐘級);health 從 503 轉 200:
  docker exec anila-model-asr-decoder curl -sf http://localhost:9000/health
  #   {"status":"ok","model":"large-v3","device":"cuda","ready":true}
  ```
  卡在 503 不轉 200 → 見 §5 故障矩陣(多半是權重路徑錯 / `ASR_LOCAL_FILES_ONLY`
  沒開卡在下載 / VRAM 不足)。
- [ ] **D6. 起 gateway(進平台,profile:asr)**
  ```bash
  COMPOSE_PROFILES=asr docker compose -p anila-platform \
    -f infra/compose/platform.yml up -d asr-gateway
  # ⚠ 只建 asr-gateway 一個新容器,不會動到平台其他 running 容器。
  ```
- [ ] **D7. nginx 載入 `/asr/` 路由 + 麥克風放行**
  ```bash
  # anila.conf 是 bind-mount → reload 即可載入新設定,不必 recreate nginx。
  docker exec anila-nginx nginx -t        # 先驗語法
  docker exec anila-nginx nginx -s reload
  ```
  (`/asr/` location 與平台 server block 的 `Permissions-Policy: microphone=(self)`
  都在 `anila.conf` 裡,隨源碼快照一起到位。)

### 驗收(在目標機上,用對方法)

- [ ] **V1. decoder health**
  ```bash
  docker exec anila-model-asr-decoder curl -sf http://localhost:9000/health   # ready:true
  ```
- [ ] **V2. gateway health** —— ⚠ **路徑帶 `/asr` 前綴**,打 `/health` 是 404
  ```bash
  docker exec anila-nginx sh -c 'curl -sf http://asr-gateway:8200/asr/health'
  #   200 = 可用;503 = 撤銷 cache 沒 ready → 查 csp/redis(見 §5)
  ```
- [ ] **V3. 瀏覽器端到端**(唯一能驗「按麥克風→出字」的方式,需真人)
  - 用 **https** 登入平台(卡登或帳密,依部署型態)。
  - 聊天輸入框旁應出現**麥克風按鈕**。沒出現 = 前端 probe `/asr/health` 非 200
    (查 profile 有沒有開、gateway health)。
  - 按麥克風 → 講一句話 → **輸入框下方出現灰字預覽** → 停頓/再按一次 →
    **定稿進輸入框** → 可編輯後送出(走既有聊天流)。
  - ⚠ 按了跳權限錯、重登也沒用 → **先查兩件事**:① 網址是不是 https
    (IP 走 http 拿不到麥克風);② nginx `Permissions-Policy` 有沒有放行
    (D7 沒 reload 或設定被還原)。這兩個症狀跟「使用者按拒絕」一模一樣。

### 回滾

```bash
# 只停 ASR,不動平台其他服務:
docker compose -p anila-platform -f infra/compose/platform.yml stop asr-gateway
bash infra/deployment/intranet/model-serve.sh down asr-decoder
docker exec anila-nginx nginx -s reload   # /asr/ location 仍在但 upstream 沒了,
                                          # 變數式 proxy_pass 不會讓 nginx 崩,回 502
```
平台其餘部分完全不受影響。要連 `/asr/` 路由都拿掉,把 `anila.conf` 的該段註解掉
再 reload —— 但通常不必,沒開 profile 時它只是回 502,不影響別的路由。

---

## 2. 內部版 — 分項說明(§1.5 checklist 的背景補充)

> 全新 air-gapped 主機的**完整照做步驟看 §1.5**。本節是各步驟的背景/理由,
> 以及「平台已在跑、只是要加 ASR」這種**非全新**情境的精簡版(跳過 Part A–D3,
> 直接 D4 起)。

decoder 進 `anila-models` stack,gateway 走內部 network 打它,不開 host port。

### 2.1 權重(air-gap:先在有外網的機器抓,再帶進內網)

`download-intranet-models.sh` 已含 `Systran/faster-whisper-large-v3`
(CTranslate2 格式,不用自己轉檔)。抓進 `ANILA_HF_DIR`:

```bash
bash infra/deployment/intranet/download-intranet-models.sh /data/staging/hf
# 產出 /data/staging/hf/faster-whisper-large-v3/(內含 model.bin)
# 帶進內網後放到 ANILA_HF_DIR 指的位置
```

### 2.2 起 decoder(GPU)

```bash
# .env(repo root)至少要有:
#   ASR_DECODER_TOKEN=<步驟 1 的密鑰>
#   ASR_GPU=<沒有 LLM 在跑的卡號>     # ⚠ 不要跟 LLM 共卡,推論延遲互擾難 debug
#   ANILA_HF_DIR=<權重目錄>
#   ASR_MODEL_SIZE=large-v3          # H100/V100 都用 float16(V100 int8 反而更慢)
bash infra/deployment/intranet/model-serve.sh up asr-decoder
```

### 2.3 起 gateway(平台 stack)

```bash
# .env 補:
#   ASR_DECODER_TOKEN=<同一把密鑰>
#   ASR_DECODE_URL=http://asr-decoder:9000   # 預設值,內部 network,免 http 旗標
COMPOSE_PROFILES=asr docker compose -p anila-platform \
  -f infra/compose/platform.yml up -d asr-gateway
```

---

## 3. 外部版(decoder 在獨立 GPU 主機,如 MLSteam/aiops)

### ⚠ 3.0 這是書面風險接受項,先簽核再部署

外部版的音訊以**明文 PCM** 走內網 http 到 GPU 主機(使用者對系統講的話 = 對話
內容,機敏)。`X-Token` 只防未授權使用 decoder,**不防竊聽**。部署前必須:

- 防火牆白名單:把 decoder 的 port 來源限縮到**平台主機 IP**,不要讓整個內網
  都打得到。
- 交換器/VLAN 層隔離 decoder 與平台之間的網段。
- **資料/資安權責人書面簽核**,記入 `docs/governance/`(比照既有風險接受流程)。

能用內部版就用內部版。

### 3.1 decoder(在 GPU 主機上)

```bash
cd services/asr-decoder
cp .env.example .env       # 填 ASR_DECODER_TOKEN、ASR_MODEL_SIZE、ASR_GPU_DEVICE_ID
# air-gap:image 先在外網 build、docker save 帶進來、docker load,再:
docker compose -f docker-compose.standalone.yml -p asr-decoder up -d --no-build
curl -sf http://localhost:9000/health
```

### 3.2 gateway(平台端指向外部主機)

```bash
# .env:
#   ASR_DECODER_TOKEN=<同一把密鑰>
#   ASR_DECODE_URL=http://<gpu-host-fqdn-or-ip>:9000
#   ASR_ALLOW_HTTP_DECODER=1     # ⚠ 跨主機 http 必須顯式開;預設 0 會擋下
COMPOSE_PROFILES=asr docker compose -p anila-platform \
  -f infra/compose/platform.yml up -d asr-gateway
```

---

## 4. 驗證(用對方法,別只看 status code)

```bash
# gateway health —— ⚠ 路徑帶 /asr 前綴,打 /health 是 404 不是服務掛了。
#   csp 容器沒 curl 的教訓 → 若要在容器內測,用 python httpx。
docker exec anila-nginx sh -c 'curl -sf http://asr-gateway:8200/asr/health'
#   200 = 可用;503 = revocation cache 沒 ready(見 §5 故障矩陣)

# decoder health(內部版)
docker exec anila-model-asr-decoder curl -sf http://localhost:9000/health
#   {"status":"ok",...,"ready":true} = 模型載好;large-v3 冷啟動可能分鐘級

# 前端:登入平台 → 聊天輸入框旁應出現麥克風按鈕(沒出現 = probe /asr/health
# 失敗,通常是 profile 沒開或 health 非 200)。按下 → 講話 → 框下出現預覽 →
# 停頓/按停 → 定稿進輸入框。這一步需要真人 + secure context,無法自動化。
```

---

## 5. 故障矩陣(「ASR 不能用」排查,由上到下)

| 症狀 | 最可能原因 | 處置 |
|---|---|---|
| 聊天框沒有麥克風按鈕 | probe `/asr/health` 非 200 | 確認 `COMPOSE_PROFILES=asr` 有開、gateway 起來了 |
| 按按鈕跳權限錯,重登也沒用 | ① 頁面非 https ② nginx `Permissions-Policy` 沒放行麥克風 | 兩個都查(症狀相同);§0 |
| WS 一連就被拒(4503) | **revocation cache 沒 ready** | 查 Redis 與 csp:**fail-closed 設計 → Redis 或 csp 一倒,ASR 全斷**。這兩個上游是第一排查對象 |
| WS 4401 | 權杖無效/過期/被撤銷,或 cookie 名不對 | 確認 `COOKIE_SECURE` 與部署一致(dev/prod cookie 名不同) |
| 講話沒出字,gateway log 有 decode error | decoder 掛了或連不上 | 查 decoder health;外部版查防火牆/`ASR_ALLOW_HTTP_DECODER` |
| decoder `/health` 一直 503 | 模型還在載(large-v3 分鐘級)、或權重路徑錯、或 air-gap 沒開 `ASR_LOCAL_FILES_ONLY` 卡在下載 | 看 decoder log |
| decoder 起不來,log 說 `ASR_DECODER_TOKEN must be set` | 刻意的 fail-loud | 填密鑰 |
| gateway 連 decoder 得到 `WRONG_VERSION_NUMBER` | `ASR_DECODE_URL` 填了 https 但外部版是純 http | 改回 http + 開旗標 |

⚠ **fail-closed 是刻意的安全姿態,不是 bug**:寧可 ASR 全斷,也不讓被撤銷的
權杖在 Redis/csp 故障期間繞過撤銷清單。排查時把 Redis 與 csp 的健康放在最前面。

---

## 6. 停用

```bash
docker compose -p anila-platform -f infra/compose/platform.yml stop asr-gateway
bash infra/deployment/intranet/model-serve.sh down asr-decoder   # 內部版
```

沒開 `COMPOSE_PROFILES=asr` 的部署完全不受影響(nginx 用變數式 proxy_pass,
upstream 不存在也照常啟動)。

---

## 7. 分支同步

功能在 `main` 驗完後,依 `AGENTS.md` §3.5 順序 cherry-pick downstream。ASR
以「預設關」姿態新增,**不覆蓋各分支 `.env.example` 的旗標姿態**。nginx 的兩處
改動(`Permissions-Policy` 放寬、`/asr/` location)隨七分支同步 —— 漏同步的
症狀:該分支部署後麥克風拿不到、或 nginx `/asr/` 404。
