# 內網部署 Runbook (prod-intranet-card)

> **Owner**:部署負責人 (`<NCSIST_EMPLOYEE_ID>`)
> **目標環境**:中科院內網,平台主機 `10.53.100.15`；主平台為 **`https://anila.ai.ncsist.org.tw`**，工具使用 `n8n`／`gitlab`／`code.ai.ncsist.org.tw` 三個獨立 origin。
> **模型來源**:`https://aiagent2.ai.ncsist.org.tw` (=10.53.100.12,My-OpenAI-Frontend gateway,模型容器不開 port)
> **更新**:2026-07-11 — Gate 0 卡登、工具隔離、資料版本護欄、repo 外私鑰與
>   fail-closed deployment acceptance。基礎交付版仍為 V1.0.0。
> **配套檔**:[`.env.example`](../../.env.example) / [`compose.yaml`](../../compose.yaml)(root shim,實體在 [`infra/compose/platform.yml`](../../infra/compose/platform.yml)) / [`infra/deployment/intranet/build-and-export-for-intranet.sh`](../../infra/deployment/intranet/build-and-export-for-intranet.sh) / [`infra/deployment/scripts/deploy-prod.sh`](../../infra/deployment/scripts/deploy-prod.sh)

這份是「**從外網 dev 機 → 帶進內網一鍵跑起來**」的逐步操作手冊。卡住直接看「Troubleshooting」段。
> **第一次部署?** 先讀精簡版主路徑 [`intranet-zero-to-prod-guide.md`](intranet-zero-to-prod-guide.md)
> (從 0 到上線 + 日常維運 `anila-ops.sh`),本檔當它的深入對照。

---

## V1.0.0 重要變更 (2026-06-12,部署前必讀)

1. **必須「重新打包」image**:之前 export 的 tar 是舊碼。V1.0.0 多了卡登真驗證、
   8 項安全 hardening、CA bundle、`asn1crypto` 依賴、0040 migration 修補 →
   **務必以 `prod-intranet-card`(tag v1.0.0) 重跑 build-and-export** 再帶進內網。
2. **卡登變「真驗證」**:不再只是解析 PKCS#7。現在驗 CMS 簽章 + 驗憑證鏈到釘死的
   中科院 **CSPKI CA** + 綁 nonce。CA bundle(`services/csp/app/services/
   cspki_ca_bundle.pem`)**已 commit 隨碼附帶**,不用手動下載。**它跟 §2.2 的
   `share/pki/model-ca.pem` 其實是「同一套 CSPKI CA」、只是用途不同**:後者驗「https
   模型 gateway 的 TLS」,前者驗「卡片簽章」——內網 PKI 卡與伺服器憑證都由 CSPKI 簽,
   所以 `[2/7]` 直接拿這個 bundle 當 model-ca(免下載、離線即有)。`asn1crypto` 已在 requirements,build 時
   自動進 csp image。
3. **`CARD_DEV_SKIP_NONCE_BINDING` 內網一律不可設**:那是 dev 用固定 mock(簽不出
   新 nonce)測試才開的旗標。內網用實體卡 → 全 nonce 綁定,別設這個。
4. **migration 0040 已修**:之前 fresh DB 跑 `0001→0045` 會卡在 0040(對無 SSO
   分支 `ALTER TABLE auth_providers` 表不存在直接炸)→ csp crash-loop。已改成表不
   存在就 no-op。**沒有這個修補,內網首次部署的 DB 會壞**(本版已含)。
5. **內網 `.env` 維持 strict**:`ANILA_ALLOW_DEV_SECRET=0`、`ANILA_ALLOW_HTTP_ENDPOINT=0`
   (模型走 https gateway)、真密碼。**別把外網 dev 機放寬過的 `.env` 帶進內網。**
6. **第一版 gateway-only**:模型走 .12 gateway,不跑本地模型/權重。`.env.example` 已配:
   `LLM_MODEL=openai/gpt-oss-20b`、embedding `nvidia/nv-embed-v2`、`GEMMA4_BASE_URL=`/
   `FLUX_AGENT_BASE_URL=` 留空(auto_seed 自動跳過,不需本地權重)。**你只需在 .12 簽
   發後填 `MODEL_GATEWAY_API_KEY`**。權重日後到了再開 gemma4/flux 即可,架構不變。
7. **JWT 簽章金鑰(2026-06-15 live 預演抓到)**:csp 用 RSA 私鑰簽登入 access token
   並對 anila-studio 等發 JWKS。prod 模式 `ALLOW_AUTO_KEYGEN=false` **不自動生**;
   缺這把 → csp `/.well-known/jwks.json` 回 500、**登入發不了 token、anila-studio
   crash-loop**。**已修**:`intranet-deploy.sh` 步驟 `[4b]` 會用 csp image 跑
   `scripts/generate-jwt-keypair.py` 產 `ANILA_SECRETS_DIR/jwt-{private,public}.pem`,
   compose 以 `:ro` mount 進 csp `/app/secrets`。`ANILA_SECRETS_DIR` 必須是 repo
    外的絕對路徑；gitignore 不是 secret boundary。手動 `docker compose up` 而沒先跑
    腳本的話,也必須先產 key 並設定該路徑。
8. **Gate 1 F6 posture + image lock**:`ANILA_DEPLOYMENT_PROFILE=prod-intranet-card`
   是可執行合約；card/public-share/memory/cookie/migration/dev-bypass 任一旗標漂移，CSP
   在 migration 前拒絕啟動。`[4]` load 完會把 checksummed
   `PLATFORM-IMAGE-LOCK.tsv` 的 13 個 default service content ID 寫成
   `ANILA_IMAGE_*=sha256:...`；後續 `deploy-prod.sh` 會重驗 wiring、本機 image ID 與
   `.env`，不得手改回 mutable tag。
   本次 Gate 1 posture/lock 合約的核准範圍只含 `prod-intranet-card` 與其限時
   break-glass profile；public/military profile 尚未審查，不得直接 port 這份 formal
   Compose，必須先另建 profile contract、bundle lock lifecycle 與 CI 證據。

> **一條龍部署 (推薦)**:不想逐步跑 §2.2–§2.3,直接在 prod-intranet-card repo 根目錄:
> ```bash
> bash infra/deployment/intranet/intranet-deploy.sh [image包資料夾]
> ```
> 互動式跑完 **TLS 抽取 → 模型 CA → 產 .env(自動生 secret + 問 gateway key / owner 工號)
> → load image + content-ID pin → JWT 金鑰 → up → 驗證**。重跑安全(偵測既有 .env 預設保留 secret,不重生 DB 密碼)。
> 底下 §2.2–§2.3 是它每一步的詳解 / 手動備援。

> **部署後兩件營運必做(live 預演 critic 抓到):**
> 1. **首登 bootstrap**:owner(工號 `<NCSIST_EMPLOYEE_ID>`,插卡直接登入)登入後**要先建 department**,
>    否則同仁卡片註冊時「完成註冊」的單位下拉是空的、卡在註冊。先建單位再請大家註冊。
> 2. **break-glass(讀卡機/HiPKI 掛掉時的後路)**:card-only 模式關掉了帳密登入,若 go-live
>    當天讀卡機或 HiPKI(`localhost:16888`)故障會**全員進不去**。不得改
>    `REQUIRE_CARD_LOGIN_ONLY=false`（F6 會把它視為 profile drift 並拒啟）；必須維持
>    `REQUIRE_CARD_LOGIN_ONLY=true` 並切到
>    `ANILA_DEPLOYMENT_PROFILE=prod-intranet-card-breakglass`，填具名
>    `ANILA_BREAK_GLASS_OWNER`、`ANILA_BREAK_GLASS_TICKET` 與未來 24 小時內的 RFC3339
>    `ANILA_BREAK_GLASS_EXPIRES_AT`，recreate CSP 後只允許 owner 帳密處理。到期後 login、
>    access 與 refresh 會即時 fail-closed；修復後仍應立即執行
>    `anila-ops.sh break-glass off`，恢復 `prod-intranet-card` 並清除事件 metadata。

---

## 0. 架構摘要 (一頁懂)

```
員工 PC                         平台主機 (10.53.100.15)              模型主機 (10.53.100.12)
─────────────────               anila.ai.ncsist.org.tw               aiagent2.ai.ncsist.org.tw
[實體卡 + 讀卡機]               ─────────────────────────            ─────────────────────────
   ↑↓ ISO-7816 APDU
[HiPKI 本機元件]
 (localhost:16888)
   ↑↓ HTTP
[瀏覽器 popup → main]
   ↑↓ HTTPS (wildcard cert)
                    ──────→     nginx :443
                                    ↓
                                csp (FastAPI)
                                 ├─ /api/auth/card/* (PKCS#7)
                                 ├─ /api/* (control plane)
                                 └─ /v1/*  ── Bearer key ──→  nginx :443 /v1 (正式憑證)
                                    ↓                              ↓
                                postgres + redis                backend → gpt-oss-20b
                                + ingestion-worker                        nv-embed-v2 …
```

**Trust 邊界**:
- 入向:卡片硬體 + PIN + HiPKI 簽出 PKCS#7。**Backend 真驗證**(V1.0.0 起):驗 CMS
  簽章 → 驗鏈到釘死的中科院 CSPKI CA(`cspki_ca_bundle.pem` 內建)→ 綁 nonce,過了
  才抽 `serialNumber`(員工編號)。不打 OCSP/CRL(離職靠實體回收 + `is_active`)。
- 出向:csp → 模型 gateway 走 https (CSPKI CA 驗證,`model-ca.pem`) + `MODEL_GATEWAY_API_KEY` (Bearer)。
  key 只注入 model 呼叫,agent dispatch 不帶 (`proxy_service._apply_gateway_auth`)。

---

## 1. Phase 0:外網準備 (在這台 dev 機跑)

### 1.1 前置確認清單

| 項目 | 取得方式 | 備註 |
|---|---|---|
| 內網 DNS A records：`anila`、`n8n`、`gitlab`、`code.ai.ncsist.org.tw → 10.53.100.15` | 請 IT 加 | 未完成前僅用 hosts／`curl --resolve` 測試，不可把工具改回主站 subpath |
| `*.ai.ncsist.org.tw` wildcard 憑證 + 私鑰 | **已持有** — `server.pfx` (空密碼,2029 到期) | 抽取指令見 §2.2;⚠ pfx 空密碼放 repo 是冒充風險,進場後改妥善保管 |
| model gateway 出向 CA (給 csp 信任 aiagent2) | **已隨碼附帶** — `cspki_ca_bundle.pem`(CSPKI Root CA G1 + 中科院憑證管理中心);`[2/7]` 自動套用 | 內網 https 與卡片登入同一套 CSPKI CA → 免下載/免跟 IT 要 |
| 模型 gateway API key | 在 aiagent2 (My-OpenAI-Frontend) 管理介面簽發一把 ANILA 專用 key | 填 `MODEL_GATEWAY_API_KEY`;獨立一把方便撤銷/歸戶 |
| HiPKI 元件預載到員工 PC | 確認 `localhost:16888` 可回應 | |

### 1.2 Build + 打包

```bash
cd $HOME/ANILA

# 基本款 (純 gateway 架構,平台主機不跑模型):
bash infra/deployment/intranet/build-and-export-for-intranet.sh

# 要在內網本機跑模型 (FLUX 繪圖 / gemma4) 就連 image + 權重一起:
WITH_MODELS=1 WITH_WEIGHTS=1 bash infra/deployment/intranet/build-and-export-for-intranet.sh
```

預期產出 (`/tmp/anila-images-export/`):

```
01-anila-built.tar.gz       (csp / ingestion-worker / router / anilalm / anila-ui / pptx-renderer)
02-base.tar.gz              (pgvector / redis / nginx)
03-cold.tar.gz              (codeserver / n8n / gitlab — 工具 image；瀏覽器走各自獨立 FQDN)
04-models.tar.gz            (WITH_MODELS=1:含 flux2-dev / anila-flux-agent / vllm-gemma4 等,數十 GB)
05-weights-*.tar            (WITH_WEIGHTS=1:預設 FLUX.2-dev 166G + gemma4 59G + assistant 0.9G)
INTRANET-LOAD.sh            (內網一鍵 import,含 sha256 驗檔 + 權重解壓)
MANIFEST.txt / CHECKSUMS.sha256
```

> **權重只能從這裡帶** — 內網無對外下載通道。image 同理 (本地客製 build,
> registry 拉不到)。
>
> **MTP 注意**:gemma4 的 `--speculative-config` 用 MTP 投機解碼,draft model
> `gemma-4-31B-it-assistant` (927MB) 是**必帶**權重 — 缺了 vLLM 直接起不來。

### 1.2b 完整模型清單下載與 Google Drive 轉入 (2026-06-10 拍板,12 repo ≈ 2469 GiB)

> **轉入通道定案 (2026-06-10):Google Drive,無轉移碟** — 單檔上限 50G。
> 格式用 tar+split 切塊 (`infra/deployment/intranet/pack-chunks.sh` / 內網端 `unpack-chunks.sh`),
> **不用 zip**(壓不動 safetensors、不能串流重組)、**不把權重塞 docker image**
> (load 要雙倍空間、壞一塊整包重傳)。chunk 45GiB:「50G」按十進位解讀時
> 48GiB 會超限。失敗域 = 單一 chunk;內網端 cat|tar 串流解壓不吃雙倍磁碟。
>
> **Google Drive 兩個硬限制**:
> 1. **上傳 750GB/帳號/天**(Google 硬上限,rclone 撞到會被 403)— 全清單+
>    本機既有+toolkit+平台 image 合計 ≈ 2.9 TiB,撐滿 quota 也要 ≥4 天。
>    → 按批次傳:批次 A(進場必要)先,B200 期貨之後分天補。
> 2. **Drive 總容量**:免費 15GB 不可能;2TB 方案也裝不下全量一次到位。
>    若收件端可分多次收:傳一批 → 收走確認 → 刪 Drive → 下一批,Drive 只需
>    容得下單批。**⚠ 待確認:內網收件是一次性還是可多次**(「通道只開一次」
>    是轉移碟時代的假設,Drive 模式下要重新跟管道方確認)。
>
> 上傳工具用 **rclone**(續傳、checksum 比對、撞日上限自動停);45G 大檔走
> 網頁拖拉容易斷且無校驗,不建議。

**批次 A(進場必要,≈ 0.8 TB ≈ 1~1.5 天 quota)**:平台 image tar.gz(§1.2)
+ 本機既有權重(gemma-4-31B-it 59G + assistant 0.9G + NV-Embed-v2 30G +
FLUX.2-dev 166G)+ 需下載的 H100 運行模型(gemma-4-12B 22G + 26B-A4B 48G +
gpt-oss-120b 182G)+ toolkit ~100G(超日上限就順延隔天)。

**批次 B(B200 期貨等,≈ 2.3 TB,之後分天傳)**:Maverick bf16/w4a16/FP8、
Mistral ×2、Scout-Instruct bf16、klein-4B、E4B,加本機既有的 gpt-oss-20b 39G、
Scout-Instruct-FP8 104G。

**逐模型 pipeline (本機只剩 ~1.3TB,不能全下完再切)**:
下載 model i → pack-chunks → rclone 上傳 → 確認後刪本地 → model i+1。

```bash
# 1. 下載單一模型到本機暫存 (腳本內 MODELS 順序已按批次優先序排好):
bash infra/deployment/intranet/download-intranet-models.sh /data/staging/hf
#    (支援中斷續傳/重跑跳過已完成;gated repo 403 → 去 HF 網頁按同意再跑)

# 2. 切塊 (每模型一組 chunk + manifest):
bash infra/deployment/intranet/pack-chunks.sh /data/staging/hf/gemma-4-26B-A4B /data/staging/chunks
#    本機既有的權重直接從 project/Huggingface 打包,不經下載:
bash infra/deployment/intranet/pack-chunks.sh $HOME/project/Huggingface/gemma-4-31B-it /data/staging/chunks
#    Maverick bf16 (748G) 專用 — 邊打包邊刪來源,峰值空間減半 (刪了不能重來,
#    chunks 落地驗過再上傳):
REMOVE_SOURCE=1 bash infra/deployment/intranet/pack-chunks.sh /data/staging/hf/Llama-4-Maverick-17B-128E-Instruct /data/staging/chunks

# 3. rclone 上傳 (撞到 750GB 日上限自動停,隔天重跑同指令續傳):
rclone copy /data/staging/chunks gdrive:anila-intranet/chunks \
  --transfers 4 --drive-chunk-size 256M --drive-stop-on-upload-limit --progress
rclone check /data/staging/chunks gdrive:anila-intranet/chunks
#    check 過了才刪本地 chunks + 暫存權重,繼續下一個模型。

# 4. 工具鏈 (llm-compressor wheelhouse + 校準資料集 + 推論伺服器 ×3,~80-100G):
bash infra/deployment/intranet/download-intranet-toolkit.sh /data/staging/toolkit
#    image tar.gz >45G 的用 pack-chunks 檔案模式切塊再上傳。
#    這包讓內網日後能自給自足:B200 換裝後從 bf16 母本離線壓 NVFP4、
#    新模型用通用推論伺服器跑 (現有 model image 都是綁單一模型的客製品)。
#    推論伺服器版本 (2026-06-10 查核的穩定版,皆支援 Hopper+Blackwell):
#      vLLM    v0.22.1  → vllm/vllm-openai:v0.22.1-cu129-ubuntu2404 (22.9G)
#      Triton  2.69.0   → nvcr.io/nvidia/tritonserver:26.05-py3
#      TRT-LLM 1.2.1    → nvcr.io/nvidia/tensorrt-llm/release:1.2.1
#        (1.3.0 仍在 RC;現役 gpt-oss 的 1.3.0rc10 image 照舊帶,新部署用 1.2.1)

# 內網端 (該模型 chunk 到齊後;manifest 逐塊驗 hash 揪壞包,只重傳那包):
bash infra/deployment/intranet/unpack-chunks.sh /transfer/gemma-4-26B-A4B.manifest.sha256 models/model
```

> 空間帳:除 Maverick 外最大單模型 388G(Maverick-FP8) → 峰值 388(權重)+
> 388(chunks)=776G < 1.3TB ✓。Maverick bf16 748G 用 REMOVE_SOURCE=1 →
> 峰值 ~793G ✓。

清單 (12 repo,**instruct 定案 2026-06-10**):Llama-4 Maverick bf16(748G)+
w4a16(201G,H100 用)+FP8(388G,B200 用)/Scout-**Instruct**(202G)、gemma-4
26B-A4B/E4B/12B(48/15/22G)、gpt-oss-120b(182G)、Mistral-Medium-3.5(249G)/
Small-4(225G)、FLUX.2-dev(165G,本機已有)/klein-4B(22G)。
**gemma-4-31B-it(58G)本機已有不下載**,直接 pack-chunks 上傳。

> **量化策略** (H100 現在 / B200 未來):
> - **H100 階段只跑 31B / 26B-A4B / 12B / gpt-oss-120b / nv-embed-v2 —
>   全部原生單卡放得下,不需要任何量化**;KV cache 開 FP8 即可。
> - 其餘 (Maverick / Scout / Mistral ×2) 等 B200:bf16 母本是萬用源頭,
>   B200 上可 vLLM 動態 FP8 (`--quantization fp8`,免離線壓),或用
>   toolkit 在內網離線壓 NVFP4 (見下);Maverick 直接用帶進去的官方 FP8。
> - H100 跑不了 NVFP4 (Blackwell 硬體格式),現在帶 NVFP4 checkpoint 沒意義。

#### 內網離線壓 NVFP4 (B200 換裝後)

工具與腳本都在 bundle 裡,無網路全程可跑:

```bash
# 1. 量化環境 (帶進去的 vLLM 容器內,或同版 python):
pip install --no-index --find-links=/path/to/toolkit/wheelhouse llmcompressor datasets

# 2. 壓 (HF_HUB_OFFLINE 等離線開關腳本內建;1 張 GPU + CPU RAM ≳ 模型 bf16×1.2):
python3 infra/deployment/intranet/intranet-quantize-nvfp4.py \
  --model $ANILA_HF_DIR/Mistral-Medium-3.5-128B \
  --out   $ANILA_HF_DIR/Mistral-Medium-3.5-128B-NVFP4 \
  --calib /path/to/toolkit/calib-datasets/Open-Platypus

# 3. 驗:vllm serve <out> 起得來 + 對話一輪
```

> ⚠ **進場前必做 rehearsal**:在外網用 gemma-4-E4B (15G) 把「wheelhouse
> 安裝 → 量化腳本 → vLLM 載入結果」整條跑一遍 — llm-compressor API 各版
> 略有差異,別讓第一次執行發生在沒有網路救援的內網。
> RAM 注意:Maverick (748G) 要 ~1TB RAM 主機才壓得動;Mistral 兩隻沒問題。
>
> 少量權重 (≤幾百 GB) 仍可用 §1.2 的 `WITH_WEIGHTS=1` tar 流程。

### 1.2c 進內網前本地演練 (R0–R2,2026-06-10 拍板)

進場前在 dev 機把「打包 → 切塊 → (通道) → 解包 → load → 啟動」整條走一遍。
範圍 = 現行 ANILA stack(兩包:①平台 image tar.gz ②models/model 權重+推論
伺服器 image),超大模型不參與演練。

| 階段 | 內容 | 前置條件 |
|------|------|----------|
| **R0** | 5 組權重 pack-chunks + 6 個模型 image → `04-models.tar.gz`(pigz)→ 切塊;全部 unpack 回來 `diff -r` 逐 byte 比對 | 無(純檔案系統+`docker save`,不碰 running stack)|
| **R1** | `build-and-export-for-intranet.sh` 出 01–03 tar.gz + INTRANET-LOAD.sh | ⚠ build 會重指 `anila-platform-*` tag(與 dev stack 同名)— 排進維護時段;正式包等 codex 複核+commit 後重出 |
| **R2** | 空機模擬:停 dev stack → INTRANET-LOAD.sh(sha256+load)→ `ANILA_HF_DIR=<staging>/rehearsal-hf` 起 models stack + `deploy-prod.sh` → §3 驗收清單 → 還原 dev stack(checkout trial-military 重 build) | ⚠ 需重開機修 NVIDIA driver mismatch(host NVML 掛了,新 GPU 容器起不來);維護時段 user 排 |

```bash
# R0 (背景跑,log 看進度):
bash $HOME/intranet-staging/rehearsal-r0.sh \
  > $HOME/intranet-staging/rehearsal-r0.log 2>&1 &
# 產出: intranet-staging/chunks/(上傳 Drive 的轉移物)
#       intranet-staging/rehearsal-hf/(R2 的 ANILA_HF_DIR,演練完可刪)
```

> R2 過了,同一套 chunks + bundle 直接上傳 Drive — 演練品即交付品
> (平台 01–03 例外:codex 複核後從乾淨 commit 重 build 重打包)。

### 1.3 Secret 生成 (建議到內網主機上跑)

```bash
echo "CSP_SECRET_KEY=$(openssl rand -hex 32)"
echo "CSP_SERVICE_TOKEN=$(openssl rand -hex 32)"
echo "INTERNAL_PLATFORM_API_KEY=sk-internal-$(openssl rand -hex 24)"
echo "ADMIN_PASSWORD=$(openssl rand -base64 24)"
echo "CSP_DB_PASSWORD=$(openssl rand -hex 32)"
echo "CSP_APP_DB_PASSWORD=$(openssl rand -hex 32)"
echo "CODESERVER_PASSWORD=$(openssl rand -base64 24)"
echo "N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)"
echo "GITLAB_ROOT_PASSWORD=$(openssl rand -base64 32)"
```

輸出直接進密碼管理器。**不要沿用試用機 .env 的值。**
(DB 兩把用 hex 是必要的 — 會嵌進 DATABASE_URL,避免特殊字元。)

---

## 2. Phase 1:內網首次部署

### 2.1 帶進內網的東西

1. 整個 ANILA repo (`prod-intranet-card` checkout)
2. `/tmp/anila-images-export/` 整個資料夾
3. `server.pfx` (wildcard 憑證+key;只在受控媒介與 repo 外 state 目錄處理)
4. 平台、n8n、GitLab secrets 與 n8n owner bcrypt hash (密碼管理器)

### 2.2 內網主機 (.15) 初始化

```bash
cd /opt/anila   # repo 解壓處
export ANILA_STATE_DIR=/var/lib/anila
export ANILA_TLS_CERTS_DIR=$ANILA_STATE_DIR/tls
export ANILA_SECRETS_DIR=$ANILA_STATE_DIR/secrets
mkdir -p "$ANILA_TLS_CERTS_DIR" "$ANILA_SECRETS_DIR"
chmod 700 "$ANILA_TLS_CERTS_DIR" "$ANILA_SECRETS_DIR"

# 1. TLS:從 pfx 抽 wildcard 憑證+私鑰 (pfx 密碼為空,直接 Enter / -passin pass:)
openssl pkcs12 -in /path/to/server.pfx -clcerts -nokeys -legacy -passin pass: \
  | openssl x509 > "$ANILA_TLS_CERTS_DIR/server.crt"
openssl pkcs12 -in /path/to/server.pfx -nocerts -noenc -legacy -passin pass: \
  | openssl pkey > "$ANILA_TLS_CERTS_DIR/server.key"
chmod 600 "$ANILA_TLS_CERTS_DIR/server.key"
# 驗:subject 應為 CN=*.ai.ncsist.org.tw
openssl x509 -in "$ANILA_TLS_CERTS_DIR/server.crt" -noout -subject -dates

# 2. CA:預設用 repo 內 CSPKI bundle(內網模型 https 與卡片登入「同一套」CSPKI CA)。
#    intranet-deploy.sh [2/7] 會自動 cp;手動等同下行(不必再下載 NCSISTCA):
mkdir -p share/pki
cp services/csp/app/services/cspki_ca_bundle.pem share/pki/model-ca.pem
# 驗:host 端先確認信任鏈成立(verify return code: 0)再交給容器
echo | openssl s_client -connect 10.53.100.12:443 -servername aiagent2.ai.ncsist.org.tw \
  -CAfile share/pki/model-ca.pem 2>/dev/null | grep 'verify return code'

# 3. .env
cp .env.example .env
nano .env   # 填 7 個 secret + MODEL_GATEWAY_API_KEY + CARD_INITIAL_OWNERS
            # 並打開 ANILA_MODEL_CA_FILE=/etc/anila/pki/model-ca.pem

# 4. import image (內含 sha256 驗檔 + 提示建 anila-models-net)
(cd /tmp/anila-images-export && bash INTRANET-LOAD.sh)

# 5. 把 checksummed bundle lock 原子寫進正式 .env，並驗本機 image/effective Compose
LOCK=/tmp/anila-images-export/PLATFORM-IMAGE-LOCK.tsv
while IFS=$'\t' read -r variable image_id; do
  tmp="$(mktemp .env.tmp.XXXXXX)"
  grep -vE "^${variable}=" .env > "$tmp" || true
  printf '%s=%s\n' "$variable" "$image_id" >> "$tmp"
  chmod 600 "$tmp" && mv -f -- "$tmp" .env
done < <(python3 infra/deployment/scripts/verify-compose-image-lock.py emit-env --lock "$LOCK")
python3 infra/deployment/scripts/verify-compose-image-lock.py verify-env \
  --env-file .env --lock "$LOCK" --inspect-docker
```

### 2.2b 模型主機 (.12) 確認 + API key 簽發 (有 admin,SSH 上去跑)

內網 .12 與外網複刻版同構(user 確認 2026-06-10):nginx **443**=webui+`/v1`、
**7000**=http API(`/v1`+`/apikey`)、**16888**=openwebui。`/user/login` 不經
nginx,直打 backend 容器(內部 port 3000)。

```bash
# A. 盤點:port 與容器(預期 443 / 7000 / 16888)
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
sudo ss -tlnp | grep -E ':(443|7000)\b'

# B. 模型 ID 精確大小寫 — 照抄進 ANILA .env (預期 openai/gpt-oss-20b、
#    nvidia/nv-embed-v2;ANILA 預設的 NV-embed-V2 大小寫不同,一定要對)
curl -s http://localhost:7000/v1/models | python3 -m json.tool

# C. 簽 API key (= ANILA 的 MODEL_GATEWAY_API_KEY)
BACK=$(docker ps --format '{{.Names}}' | grep -iE 'openai.*frontend' | head -1)
BIP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$BACK")
TOKEN=$(curl -s -X POST "http://$BIP:3000/user/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'username=<gateway管理帳號>' --data-urlencode 'password=<密碼>' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST "http://$BIP:3000/apikey" -H "Authorization: Bearer $TOKEN" \
  | python3 -m json.tool        # ← 整串 key 進密碼管理器
# (host 連不到容器 IP 的備案:docker exec "$BACK" 在容器內 curl localhost:3000)

# D. 用 key 走 ANILA 實際路徑驗證 (443 + Bearer)
KEY=<上一步的key>
curl -sk https://localhost/v1/models -H "Authorization: Bearer $KEY" | head -c 300; echo
curl -sk https://localhost/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'
curl -sk https://localhost/v1/embeddings -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"nvidia/nv-embed-v2","input":"ping"}' | head -c 300; echo

# E. CA chain (§2.2 的官方下載點不通時的備案:從 gateway 自己抓)
openssl s_client -connect localhost:443 -showcerts </dev/null 2>/dev/null \
  | awk '/BEGIN CERT/,/END CERT/' > /tmp/gw-chain.pem
openssl crl2pkcs7 -nocrl -certfile /tmp/gw-chain.pem \
  | openssl pkcs7 -print_certs -noout   # 看幾張、issuer;leaf 以外的就是 chain
```

D 全綠 = port/key/模型 ID/TLS 四件事一次確認完。F(FQDN 解析)要在 .15 上
跑:`getent hosts aiagent2.ai.ncsist.org.tw`(見 §3.3)。

### 2.3 `.env` 關鍵值速查 (完整範本見 `.env.example`,已是內網拓撲)

```bash
ANILA_ALLOW_DEV_SECRET=0          # prod 模式,dev 預設值一律拒啟
ANILA_ALLOW_HTTP_ENDPOINT=0       # 模型走 https,不用開
ANILA_ALLOW_PRIVATE_ENDPOINT=0
ANILA_TRUSTED_HOSTS=aiagent2.ai.ncsist.org.tw   # FQDN 解到私網 IP,要點名放行

ANILA_HOST=anila.ai.ncsist.org.tw
ANILA_SECRETS_DIR=/var/lib/anila/secrets
ANILA_TLS_CERTS_DIR=/var/lib/anila/tls
N8N_HOST=n8n.ai.ncsist.org.tw
N8N_EDITOR_BASE_URL=https://n8n.ai.ncsist.org.tw/
N8N_WEBHOOK_URL=https://n8n.ai.ncsist.org.tw/   # canonical only; nginx 仍封鎖 /webhook*
N8N_OWNER_EMAIL=<正式管理者信箱>
N8N_OWNER_PASSWORD_HASH='$2b$12$...'              # 完整 bcrypt；單引號保留 $
N8N_ENCRYPTION_KEY=<openssl rand -hex 32>        # 既有 volume 必須沿用原 key
GITLAB_HOST=gitlab.ai.ncsist.org.tw
GITLAB_ROOT_PASSWORD=<openssl rand -base64 32>   # 僅 fresh install 生效
CODESERVER_HOST=code.ai.ncsist.org.tw
ENABLE_CARD_LOGIN=true
REQUIRE_CARD_LOGIN_ONLY=true
CARD_INITIAL_OWNERS=<NCSIST_EMPLOYEE_ID>  # 實際 owner 員工編號;多人用 CSV

ANILA_REMOTE_MODELS=1             # deploy-prod.sh preflight 改 curl 遠端探測
LOCAL_LLM_MODEL=openai/gpt-oss-20b              # gateway 的 RESPONSE_ID,大小寫敏感
LOCAL_LLM_BASE_URL=https://aiagent2.ai.ncsist.org.tw
LOCAL_EMBEDDING_MODEL=nvidia/nv-embed-v2        # ≠ 預設 nvidia/NV-embed-V2!
LOCAL_EMBEDDING_BASE_URL=https://aiagent2.ai.ncsist.org.tw
MODEL_GATEWAY_API_KEY=<在 aiagent2 平台簽發>
ANILA_MODEL_CA_FILE=/etc/anila/pki/model-ca.pem

GEMMA4_BASE_URL=                  # 空 = 內網無 gemma4,auto_seed 跳過
LLM_MODEL=openai/gpt-oss-20b      # Router primary
ANILALM_DEFAULT_CHAT_MODEL=openai/gpt-oss-20b
FLUX_AGENT_BASE_URL=              # 空 = 無 FLUX,繪圖 agent 不註冊
FLUX_BACKEND_URL=                 # 空 = studio 圖像 pipeline 停用
ENABLE_IMAGE_CAPTIONS=false       # 內網無 VLM,文件圖片以 [image] 處理
```

---

## 3. Phase 2:首次 boot + 驗證

### 3.1 Preflight + 啟動

```bash
cd /opt/anila
set -a; source .env; set +a
bash infra/deployment/scripts/deploy-prod.sh preflight   # 遠端模型模式:自動建 anila-models-net
                                        # + curl 探測 gateway (帶 Bearer key)
docker compose up -d --no-build --pull never  # image 已 load且 content-ID locked
```

### 3.1b 本機模型要過 url_guard (R2 演練教訓,2026-06-11)

> **純 gateway 模式(模型全在 aiagent2 https 後面)跳過本節,flag 維持 0。**
> 跑本機模型/混合模式(§4.5)時,`http://單label容器名:8000` 端點會被
> S-117 url_guard 擋 — 症狀:**registry 全 offline + `/v1/embeddings` 等
> 資料面 7ms 即回 502**,health_checker log 出現 `unsafe endpoint`。
> 放行需要**兩件事都做**(guard 設計:scheme 檢查獨立於 trusted,
> trusted 只繞 host 檢查):
> 1. `.env` 設 `ANILA_ALLOW_HTTP_ENDPOINT=1`(容器間裸 http 的 on-prem
>    例外,url_guard 註解明文支持;`ALLOW_PRIVATE` 維持 0)
> 2. owner 把容器名加進 trusted-hosts(單 label 名過不了 host 檢查):

```bash
# owner 登入(卡片,或 break-glass 帳密)拿 token 後:
for h in nv-embed-proxy gemma4 gpt-oss-20b flux2-dev-agent; do
  curl -sk -X POST https://localhost/api/trusted-hosts \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"host\":\"$h\",\"note\":\"本機模型容器\"}"
done
# 等下一輪 health check (~60s) → /models 頁應全轉 online
# 驗收:R2 實測此組態下 registry 4/4 online、embedding 經 csp 200 (4096 維)
```

### 3.2 startup_security 一定要過

```bash
docker compose logs csp 2>&1 | grep -E "startup_security|RuntimeError|Refusing"
```

| 看到的 log | 處理 |
|---|---|
| 沒輸出 | 正常,繼續 |
| `Refusing to start: ... dev 預設值: SECRET_KEY/ADMIN_PASSWORD/DB_PASSWORD...` | 對應 secret 沒換真值 (§1.3 重生) |
| `REQUIRE_CARD_LOGIN_ONLY=True 但 ENABLE_CARD_LOGIN=False` | 兩個 flag 要一致 |
| compose 階段 `required variable XXX is missing` | .env 漏填,csp 根本沒起 |

### 3.3 健康檢查 + 模型鏈路

```bash
curl -k https://localhost/health                       # 200
docker compose ps                                       # 全部 healthy
# 模型 e2e (在 host,key 換真值):
curl --cacert share/pki/model-ca.pem \
  -H "Authorization: Bearer $MODEL_GATEWAY_API_KEY" \
  https://aiagent2.ai.ncsist.org.tw/v1/models           # 應列出 openai/gpt-oss-20b 等
# csp 容器內 DNS 解析確認:
docker compose exec csp python -c "import socket; print(socket.gethostbyname('aiagent2.ai.ncsist.org.tw'))"
# 解不到 → compose csp 加 extra_hosts: "aiagent2.ai.ncsist.org.tw:10.53.100.12"
```

---

## 4. Phase 3:首次 admin ops (一次性)

### 4.1 你的卡片登入

1. 瀏覽器打 `https://anila.ai.ncsist.org.tw/login` — **不應有任何憑證警告** (有 = cert 沒換到 wildcard 或 DNS 沒生效)
2. 只看到「auth · pki card」區塊 (本機帳密 / OIDC 已 lockdown)
3. detect card → 顯示姓名/員編 → 輸 PIN → sign & submit
4. `CARD_INITIAL_OWNERS` 內的員編直接以 `role=owner` 進入

### 4.2 預先建立 departments

`/departments` 建好單位 — 同事首刷的「完成註冊」表單要從 dropdown 選,沒建會是空的。

### 4.3 確認 model 註冊

`/models` 應看到 **`openai/gpt-oss-20b`** (LLM) + **`nvidia/nv-embed-v2`** (embedding),健康綠。
**不應**看到 gemma4 或 image-generator (空 endpoint 已被 auto_seed 跳過;出現 = .env 沒設空)。

### 4.4 端到端驗收

1. 對話:新會話用 gpt-oss-20b 問一題,串流正常、token 用量有記錄
2. RAG:上傳一份 PDF → ingestion 完成 → 引用查詢命中 (走 nv-embed-v2)
3. 同事流程:首刷 → pending → `/users` approve → 二刷進入

---

## 4.5 (選配) 本機模型混合模式 — FLUX 繪圖 / gemma4 回歸

**前提:平台主機有 GPU**(FLUX 要 2 張、gemma4 要 1 張,見 `infra/models/docker-compose.yml`
的 `device_ids`,依內網主機 GPU 配置調整)。

架構:LLM/embedding 繼續走 aiagent2 gateway,FLUX(+gemma4)在本機跑。

```bash
# 1. 權重放 <repo>/models/model (= ANILA_HF_DIR 預設;INTRANET-LOAD/rsync
#    的目的地指這裡)。放別處就在 .env 設 ANILA_HF_DIR。

# 2. 起模型 stack (獨立 compose project;腳本會自動 source .env + 建 network)
cd /opt/anila
bash infra/deployment/intranet/model-serve.sh up flux2-dev flux2-dev-agent  # 要 gemma4 就加上
# 內網 H100 完整組 (gemma4/A4B/12B/120B/nv-embed) 一鍵:
# bash infra/deployment/intranet/model-serve.sh up intranet

# 3. 平台 .env 把對應變數從「空字串」改回「不設」(刪掉或註解),
#    讓 compose 預設的 docker DNS 名生效:
#    FLUX_AGENT_BASE_URL=   → 刪除該行 (恢復 http://flux2-dev-agent:8000)
#    FLUX_BACKEND_URL=      → 刪除該行 (恢復 http://flux2-dev:8000)
#    GEMMA4_BASE_URL=       → 跑了 gemma4 才刪;同時可開回:
#    ENABLE_IMAGE_CAPTIONS=true + VISION_MODEL=gemma4 (圖表進 RAG)

# 4. 重建平台 csp 讓 auto_seed 重新註冊
cd /opt/anila && docker compose up -d --no-build --pull never csp
# /models 應出現 image-generator (圖像繪製);對話輸入「畫一張…」驗證 dispatch
```

---

## 5. Phase 4:後續維運

### 5.1 n8n / GitLab / code-server 開發環境

**⚠ 2026-07-26（W1-8）起，三個工具都是 opt-in**：n8n 與 GitLab 先前沒有
`profiles:`，所以照本 runbook 部署會**預設把它們一起拉起來** —— 兩者都有自己的
認證邊界（n8n 原生 user-management、GitLab 的 initial root password），
都不吃 CSP 的 `auth_request`，等於在平台旁邊多開兩個獨立認證面；而交付規格
要求移除開發者工具的軍用分支也共用同一份 `platform.yml`。現在三者同屬
`developer-tools` profile，不帶 profile 的 `up` 只會起 10 個平台服務。

要用工具就顯式帶 profile：

```bash
# n8n / GitLab（https://n8n.ai.ncsist.org.tw/、https://gitlab.ai.ncsist.org.tw/）
docker compose -p anila-platform -f infra/compose/platform.yml \
  --profile developer-tools up -d n8n gitlab

# code-server：先準備「獨立 clone」，不要把正式 repo root / .env / secrets 放進去
git clone <internal-gitlab-url> share/codeserver-sandbox/anila-dev
bash infra/deployment/scripts/deploy-prod.sh codeserver-up
# 使用完畢
bash infra/deployment/scripts/deploy-prod.sh codeserver-down
```

> `deploy-prod.sh` 沒有用 `--remove-orphans`，所以這個改動**不會**把 `.15` 上
> 已經在跑的 n8n / GitLab 停掉；它只改變「下一次全新 `up`」的預設。要停的話
> 顯式 `--profile developer-tools stop n8n gitlab`。
> 附帶效果：`cmd_codeserver_up` 的 Gate 2 pilot 禁令是綁 `developer-tools`
> 這個 profile 的語意，n8n / GitLab 收進同一 profile 後姿態才一致。

code-server URL 為 `https://code.ai.ncsist.org.tw/`。三個工具使用各自原生認證；
主平台 `/codeserver*`、`/n8n*`、`/gitlab*` 的 404 是安全邊界，**不可刪除**。
n8n `/webhook*` 同樣維持 404，除非另行完成 machine-ingress 安全審查。
部署前須確認四個 FQDN 都解析到平台主機，且 TLS 憑證 SAN 涵蓋四者。

正式 compose 目前固定 `n8nio/n8n:2.29.10` 與
`gitlab/gitlab-ce:19.1.1-ce.0`。`deploy-prod.sh` 會在看到舊 image container、
或沒有目標版本驗證標記的既有資料卷時停止，**不得刪 guard 或直接讓新 major
image 啟動舊資料**：

- n8n 先備份，依官方 v2 migration guide 完成相容性處理，並確認既有
  `N8N_ENCRYPTION_KEY` 能解密 credentials。
- GitLab 必須依 [required upgrade stops](https://docs.gitlab.com/update/upgrade_paths/)
  逐站升級；16.10.x 起至少經 16.11.10、17.3.7、17.5.5、17.8.7、17.11.7，
  後續版本依當下官方路徑與每站 background migration 狀態決定。
- 目標版本啟動後執行 `deploy-prod.sh wait` 與 `verify`；只有全部通過才會寫入
  資料卷版本標記，之後的 `down`／`up` 才能安全復原。

### 5.2 TLS cert rotation

wildcard 憑證換發請執行 `anila-ops.sh cert-renew <server.pfx>`，輸出固定在 repo 外
`ANILA_TLS_CERTS_DIR`；驗證通過後由腳本 reload nginx。
NCSIST CA 換代時同步更新 `share/pki/model-ca.pem` 並 `docker compose restart csp`。

### 5.3 Postgres backup

```bash
docker exec anila-platform-csp-db-1 pg_dump -U csp csp | gzip > /backup/anila-$(date +%Y%m%d).sql.gz
```

### 5.4 加 owner / 模型 gateway key 輪替

- 新 owner:改 `CARD_INITIAL_OWNERS` (只對新刷卡者生效) 或 `/users` UI 改既有帳號
- gateway key 輪替:aiagent2 重簽 → 執行 `anila-ops.sh gateway-key`（內含 no-build recreate + readback）

---

## 6. Troubleshooting

### 6.1 啟動失敗

| 症狀 | 原因 | 處理 |
|---|---|---|
| compose 報 `required variable XXX is missing` | .env 漏 secret | 補填 |
| csp 立刻 exit | startup_security raise | `logs csp` 看 RuntimeError |
| csp 無限重啟 exit 3、log **零錯誤**只有 alembic fallback WARN | alembic 鏈斷 → create_all fallback → migration 才會建的東西(如 0014 的 `csp_app` role)沒生出來 → lifespan 連 DB 死 (pg scram 對不存在 role 也回 `password authentication failed`) | 別被 fallback 騙;`docker logs csp \| grep -i alembic` 找斷鏈原因。R2 演練(2026-06-11)就靠這個抓到重複 0035 與 schema 漂移 → 修法=刪殘留 migration + 0040 對齊 |
| codeserver `EACCES /home/coder/.config` 重啟循環 | compose 把它跑成 `user: ${UID:-1001}`(host 使用者),但 fresh volume 初始化繼承 image 的 coder=1000 → uid 對不上 | 冷服務不擋驗收;修:`docker run --rm -v <project>_codeserver_config:/m alpine chown -R 1001:1001 /m && docker restart <codeserver容器>`(uid 跟著 compose 的 user: 值走) |
| nginx `cannot load certificate` | cert 沒放好 | 確認 `certs/server.{crt,key}` + key mode 600 |

### 6.1b GPU 容器 error 803 (driver 升級後)

| 症狀 | 原因 | 處理 |
|---|---|---|
| 全部 GPU 容器 `CUDA error 803: unsupported display driver / cuda driver combination`,host `nvidia-smi` 卻正常 | driver 升級後舊版 userspace 庫殘留(不屬任何套件),container toolkit 注入到舊檔 | `dpkg -S /usr/lib/x86_64-linux-gnu/libcuda.so.<舊版號>` 查無歸屬即孤兒 → `sudo find /usr/lib/x86_64-linux-gnu -name '*<舊版號>*' -delete && sudo ldconfig` |
| 清完孤兒庫後新容器 create 直接炸 `failed to fulfil mount request` | CDI spec 是開機時舊狀態生成的快取 | `sudo nvidia-ctk cdi generate --output=/var/run/cdi/nvidia.yaml` |
| 修完 toolkit 後容器照樣 803 | 既有容器的 mount spec 在 create 時就固定了 | `docker start` 沒用,必須 **recreate**(`compose up -d --force-recreate`) |
| 模型容器 healthy 但推論才炸 GPU 錯 | lazy-load 服務(如 FLUX)health check 不碰 CUDA | healthy ≠ GPU 可用,進場驗收一定要打一次真推論 |

### 6.2 模型不通 (本次新拓撲最常見)

| 症狀 | 原因 | 處理 |
|---|---|---|
| `CERTIFICATE_VERIFY_FAILED` | CA 沒掛好 | §2.2 步驟 2 重做;確認 `.env` 開了 `ANILA_MODEL_CA_FILE` 且檔案在 `share/pki/` |
| hostname mismatch | BASE_URL 用了 IP | 一律用 `https://aiagent2.ai.ncsist.org.tw` |
| `401/403` | gateway key 沒帶到 / 失效 | 確認 `MODEL_GATEWAY_API_KEY` 已設並重建 csp;在 aiagent2 平台確認 key 有效 |
| `404` model not found | model 名大小寫錯 | 必須 `openai/gpt-oss-20b` / `nvidia/nv-embed-v2` (gateway RESPONSE_ID) |
| DNS 解不到 aiagent2 | 容器 DNS 沒繼承到 | compose csp `extra_hosts` 釘 `10.53.100.12` |
| url_guard 擋 (`private IP`) | trusted 沒設 | `ANILA_TRUSTED_HOSTS=aiagent2.ai.ncsist.org.tw` |

### 6.3 卡片登入失敗

| 症狀 | 原因 | 處理 |
|---|---|---|
| 「尚未安裝中華電信本機元件」 | HiPKI 沒跑 | 員工 PC `curl localhost:16888/popupForm` 應回 HTML |
| sign & submit 後 PIN 錯誤 | PIN / 卡片 | 重插卡再試 |
| 登入後彈回 /login | cookie / cert hostname | 用 FQDN 連;devtools 看 cookie |

### 6.4 同事 stuck 在 pending

| 症狀 | 處理 |
|---|---|
| 「請選擇單位」dropdown 空 | admin 先建 `/departments` |
| 完成註冊一直 loading | registration_token 15min 過期,重新刷卡 |
| `/users` 看不到 pending | filter 包含 Pending 狀態 |

---

## 7. 給 IT / aiagent2 管理側的清單

1. **DNS**:`anila`、`n8n`、`gitlab`、`code.ai.ncsist.org.tw → 10.53.100.15` A records
2. **gateway API key**:在 aiagent2 簽發 ANILA 專用一把
3. **NCSIST CA**:確認 `repository.ncsist.org.tw/certs/NCSISTCA.cer` 內網可達 (不行就從公司 PC 匯出)
4. **防火牆**:.15 的 443 對員工網段開;.15 → .12 的 443 互通
5. **HiPKI 元件**:員工 PC 版本一致,listen `localhost:16888`
6. **wildcard pfx 保管**:空密碼 pfx 不入 repo / 共用碟;評估是否通報憑證中心

---

## 8. Cross-reference

- 出向 gateway key 注入:[`proxy_service._apply_gateway_auth`](../../services/csp/app/services/proxy_service.py) + [`tests/test_gateway_auth.py`](../../services/csp/tests/test_gateway_auth.py)
- 空 endpoint = 停用:[`auto_seed.py`](../../services/csp/app/services/auto_seed.py)
- 啟動安全檢查:[`startup_security.py`](../../services/csp/app/services/startup_security.py)
- backend 卡片驗證:[`card_auth.py`](../../services/csp/app/services/card_auth.py)
- 部署腳本 (含 `ANILA_REMOTE_MODELS=1` 遠端模型模式):[`infra/deployment/scripts/deploy-prod.sh`](../../infra/deployment/scripts/deploy-prod.sh)
- mock 卡片元件:[`cht/`](../../cht/) (僅 dev,內網用真 HiPKI)
