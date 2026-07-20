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

---

## 1. 產生共享密鑰

gateway 與 decoder 靠 `ASR_DECODER_TOKEN` 互認,兩邊必須同值。**這是 PUBLIC
repo,密鑰只進 `.env`,絕不進 repo。**

```bash
openssl rand -hex 32          # 產一把,填進兩邊的 .env(見下)
```

---

## 1.5 全新 air-gapped 主機:先上平台,ASR 是最後一塊

若目標主機是**全新且完全內網**(例:先部署整個 ANILA 平台再加語音),ASR 不是
獨立部署 —— 它掛在平台上,需要 csp/redis 才能認證。順序:

1. **先把整個平台部署起來** —— 照 `docs/runbooks/intranet-zero-to-prod-guide.md`
   與 `intranet-deployment-runbook.md`(TLS、JWT keypair、卡登 CRL、DB 密碼那些
   都在那)。**本文不重複平台級步驟。**
2. **ASR 已經接進既有 air-gap 管線**(2026-07-18 登記),bundle 會自動帶上,
   但 decoder 有一個手動步驟(見下)。
3. 平台起來後,才做 §2 把 ASR 兩個服務加上去。

### 在「有外網」的打包機上(build-and-export)

```bash
# gateway:source=built,平台 bundle 會自動 build(已登記在 platform-image-inventory.tsv)
# decoder:model bundle 只 docker save 現成 image、不 build → 必須先手動 build
docker build -t asr-decoder:0.1.0 services/asr-decoder      # ⚠ 這步不能省
#   CUDA base(nvidia/cuda:12.6.3-cudnn-runtime)會烘進 image 層,docker save 一起
#   帶走,不必單獨處理 base image。

WITH_MODELS=1 bash infra/deployment/intranet/build-and-export-for-intranet.sh <輸出目錄>
#   platform bundle → 含 asr-gateway;04-models.tar.gz → 含 asr-decoder
#   打完檢查 MODEL-IMAGE-STATUS.tsv 裡 asr-decoder 是 "included" 而非 "missing"

# large-v3 權重(已登記在 download 腳本;走權重通道,不進 image bundle)
bash infra/deployment/intranet/download-intranet-models.sh <權重暫存目錄>
```

### 搬進內網、載入

把 bundle + 權重用你們的資料閘道搬到 172.16.120.35,`docker load` 各個 tar.gz,
權重放到 `ANILA_HF_DIR` 指的位置。之後才走下面 §2。

⚠ **decoder 需要那台主機有 GPU**(內部版的前提)。若目標主機沒有 GPU,內部版
在該主機不存在 —— 要改用外部版(§3),decoder 另擺一台有卡的機器。

---

## 2. 內部版(decoder 與平台同機,語音不出主機)—— 內網優先

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
