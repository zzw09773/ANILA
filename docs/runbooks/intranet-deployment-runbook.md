# 內網部署 Runbook (restart/from-redesign annotated-tag bundle)

> **Owner**:你 (1147259)
> **目標環境**:中科院內網,平台主機 `10.53.100.15`,對外名稱 **`https://anila.ai.ncsist.org.tw`**
> **模型來源**:`https://aiagent2.ai.ncsist.org.tw` (=10.53.100.12,My-OpenAI-Frontend gateway,模型容器不開 port)
> **更新**:2026-08-14 — 交付來源改為 `restart/from-redesign` 的 annotated tag;
> bundle `MANIFEST.txt` 記錄 tag+commit,內網端不 checkout branch,直接接收 bundle。
> **V1.0.0** — 卡登改真驗證、安全 hardening、migration 0040 修補、版號定版。
> 前一版 2026-06-10。
> **配套檔**:[`.env.example`](../../.env.example) / [`compose.yaml`](../../compose.yaml)(root shim,實體在 [`infra/compose/platform.yml`](../../infra/compose/platform.yml)) / [`infra/deployment/intranet/build-and-export-for-intranet.sh`](../../infra/deployment/intranet/build-and-export-for-intranet.sh) / [`infra/deployment/scripts/deploy-prod.sh`](../../infra/deployment/scripts/deploy-prod.sh)

這份是「**從外網 dev 機 → 帶進內網一鍵跑起來**」的逐步操作手冊。卡住直接看「Troubleshooting」段。

---

## V1.0.0 重要變更 (2026-06-12,部署前必讀)

1. **必須「重新打包」image**:之前 export 的 tar 是舊碼。V1.0.0 多了卡登真驗證、
   8 項安全 hardening、CA bundle、`asn1crypto` 依賴、0040 migration 修補 →
   **務必從 `restart/from-redesign` 的 annotated tag 重跑 build-and-export** 再帶進內網;
   export 的 `MANIFEST.txt` 會記錄該 tag 與 commit。
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
   並對 anila-studio 等發 JWKS。production runtime **不自動生 key**;
   缺這把 → csp `/.well-known/jwks.json` 回 500、**登入發不了 token、anila-studio
   crash-loop**。**已修**:`intranet-deploy.sh` 步驟 `[4b]` 會用 csp image 跑
   `scripts/generate-jwt-keypair.py` 產 `secrets/jwt-{private,public}.pem`,compose
   以 `:ro` mount 進 csp `/app/secrets`(`./secrets` 在 host,recreate 不失效;
   `*.pem` 已被 .gitignore 擋,不進公開 repo)。手動 `docker compose -p anila-restart -f compose.yaml -f
   intranet-image-overrides.yml up` 而沒先跑
   腳本的話,記得自己先產這把 key。

> **一條龍部署 (推薦)**:不想逐步跑 §2.2–§2.3,直接在收到的 annotated-tag bundle 配套 repo 根目錄:
> ```bash
> bash infra/deployment/intranet/intranet-deploy.sh [image包資料夾]
> ```
> 互動式跑完 **TLS 抽取 → 模型 CA → 產 .env(自動生 secret + 問 gateway key / owner 工號)
> → load image → JWT 金鑰 → up → 驗證**。重跑安全(偵測既有 .env 預設保留 secret,不重生 DB 密碼)。
> 底下 §2.2–§2.3 是它每一步的詳解 / 手動備援。

## QUESTION TREE：一條龍腳本的提問樹

`intranet-deploy.sh` 的問題不是固定序列：它會依 repo、bundle、憑證、`share/*` 和 `.env`
上是否已有產物決定哪些問題出現。不要把一串固定答案直接 pipe 給腳本；先按下面的觸發條件走分支。
從零演練的 A/B 類清除與作答規則，以 [`first-install-rehearsal.md` §1](./first-install-rehearsal.md)
為準；尤其 B 類兩題的正確答案都與預設相反。

| 問題 | 觸發條件 | 預設 | 從零／無人值守的正確處理 |
|---|---|---|---|
| `image 包資料夾路徑 (含 INTRANET-LOAD.sh)` | 沒有自動找到候選 bundle | 無 | 提供 bundle 絕對路徑；把 bundle 路徑直接當腳本參數可跳過此問。 |
| `重新從 pfx 抽取?(會覆蓋)` | `infra/nginx/certs/server.crt` 與 `server.key` 都已存在 | `N` | 從零若保留這兩個檔案，答 `y`；若先清掉，問題不出現，直接走下一列。B 類詳見初裝章 §1。 |
| `server.pfx 路徑` | 上一題選 `y`，或任一 TLS 檔不存在 | 無 | 輸入實際 `server.pfx` 路徑。 |
| `pfx 密碼 (空就直接 Enter)` | 需要抽取 TLS | 空字串 | 本包的空密碼 pfx 直接按 Enter；有密碼才輸入。 |
| `model CA PEM 路徑 ...` | `share/pki/model-ca.pem` 不存在，且 repo 內沒有 CSPKI bundle | 空字串 | 正常從零 bundle 有 CSPKI bundle，這題不出現；若真的沒有，輸入 IT 提供的 PEM，或留空並接受後續 TLS warning。 |
| `保留現有 secret?` | `.env` 已存在 | `Y` | 真正從零沒有 `.env`，這題不出現；若演練保留既有 `.env` 來行使重生路徑，答 `n`。這是 B 類，答案規則見初裝章 §1。 |
| `偵測到預設 owner ... 確定用這組?` | `CARD_INITIAL_OWNERS` 已由 process env、bundle defaults 或 `.env` 提供 | `N` | 已餵入正確 owner 時答 `y`；不採用該值則答 Enter/N，腳本會再問下一列。 |
| `CARD_INITIAL_OWNERS — owner 員工編號 ...` | 沒有確認上一列的 owner 預設 | 無 | 輸入包含自己的員編 CSV，例如 `1147259,1234567`；不可留空。 |
| `MODEL_GATEWAY_API_KEY ... (還沒有就 Enter 跳過)` | owner 設定完成後一律出現 | 空字串（Enter） | 有 `.12` key 就輸入；尚未簽發可按 Enter，之後補入 `.env` 並 recreate csp。 |
| `存好了按 Enter 繼續 ...` | 本次 `REGEN=1`，或 preserve 模式本次新生成 secret | Enter | 把畫面列出的 secret 存入密碼管理器後按 Enter；preserve 模式若沒有新生成 secret 則不出現。 |

### 非互動餵值

無人值守 runner 應用環境值與 bundle 內的 `intranet-defaults.env` 餵「值」，再依上表只回答實際被觸發的確認題；不要假設每台機器都有相同問題數。腳本直接採用的常用環境值包括：

```bash
CARD_INITIAL_OWNERS=1147259 \
COMPOSE_PROJECT_NAME=anila-restart \
INCLUDE_ASR=1 \
ASR_OVERLAY=infra/compose/asr-cpu.yml \
bash infra/deployment/intranet/intranet-deploy.sh /path/to/image-bundle
```

`COMPOSE_PROJECT_NAME` 預設 `anila-restart`；`INCLUDE_ASR` 預設 `1`，設為 `0` 才不帶 `--profile asr`。
`ASR_OVERLAY` 預設 `infra/compose/asr-cpu.yml`（.15 是 CPU 主機）；GPU 主機改設
`ASR_OVERLAY=infra/compose/asr-gpu.yml`。要不疊 overlay，設 `ASR_OVERLAY=`，這代表由操作者自行承擔組態責任；`INCLUDE_ASR=0` 時腳本不採用 overlay。
bundle 的 `intranet-defaults.env` 可提供 `CARD_INITIAL_OWNERS` 與生成器白名單內的密碼／token 值；
`CARD_INITIAL_OWNERS=...` 仍會先出現確認題，這是刻意的 owner 安全閘，不應用固定 stdin 順序繞過。

> **部署後兩件營運必做(live 預演 critic 抓到):**
> 1. **首登 bootstrap**:owner(工號 `1147259`,插卡直接登入)登入後**要先建 department**,
>    否則同仁卡片註冊時「完成註冊」的單位下拉是空的、卡在註冊。先建單位再請大家註冊。
> 2. **break-glass(讀卡機/HiPKI 掛掉時的後路)**:card-only 模式關掉了帳密登入,若 go-live
>    當天讀卡機或 HiPKI(`localhost:16888`)故障會**全員進不去**。應急:`.env` 暫設
>    `ANILA_AUTH_MODE=password` → `docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp`,用 owner 帳密
>    (admin 密碼)break-glass 進去處理,修好讀卡環境後改回 `card-only` 再 recreate csp。

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
| 內網 DNS A record `anila.ai.ncsist.org.tw → 10.53.100.15` | 請 IT 加 | 沒配好前過渡用 IP 連 (有憑證警告,預期) |
| `*.ai.ncsist.org.tw` wildcard 憑證 + 私鑰 | **已持有** — `server.pfx` (空密碼,2029 到期) | 抽取指令見 §2.2;⚠ pfx 空密碼放 repo 是冒充風險,進場後改妥善保管 |
| model gateway 出向 CA (給 csp 信任 aiagent2) | **已隨碼附帶** — `cspki_ca_bundle.pem`(CSPKI Root CA G1 + 中科院憑證管理中心);`[2/7]` 自動套用 | 內網 https 與卡片登入同一套 CSPKI CA → 免下載/免跟 IT 要 |
| 模型 gateway API key | 在 aiagent2 (My-OpenAI-Frontend) 管理介面簽發一把 ANILA 專用 key | 填 `MODEL_GATEWAY_API_KEY`;獨立一把方便撤銷/歸戶 |
| HiPKI 元件預載到員工 PC | 確認 `localhost:16888` 可回應 | |

### 1.2 Build + 打包

```bash
cd /home/aia/c1147259/ANILA

# 基本款 (純 gateway 架構,平台主機不跑模型):
bash infra/deployment/intranet/build-and-export-for-intranet.sh

# 要在內網本機跑模型 (FLUX 繪圖 / gemma4) 就連 image + 權重一起:
WITH_MODELS=1 WITH_WEIGHTS=1 bash infra/deployment/intranet/build-and-export-for-intranet.sh
```

預期產出 (`/tmp/anila-images-export/`):

```
01-anila-built.tar.gz       (csp / ingestion-worker / router / anilalm / anila-ui / pptx-renderer)
02-base.tar.gz              (pgvector / redis / nginx)
03-cold.tar.gz              (codeserver / n8n / gitlab — nginx 鎖死但保留)
04-models.tar.gz            (WITH_MODELS=1:含 flux2-dev / anila-flux-agent / vllm-gemma4 等,數十 GB)
05-weights-*.tar            (WITH_WEIGHTS=1:預設 FLUX.2-dev 166G + gemma4 59G + assistant 0.9G)
INTRANET-LOAD.sh            (內網一鍵 import,含 sha256 驗檔 + 權重解壓)
MANIFEST.txt / CHECKSUMS.sha256
intranet-image-overrides.yml   (compose up 時將 pinned image 改為已 load 的 tag-only)
```

> **code-server 權限警告**：它掛載整個 repo、可寫入工作樹，並直接掛載
> `/var/run/docker.sock`；工作樹中未被 `/dev/null` 遮蔽的 `secrets/` 也可被讀取。
> 因此取得 `CODESERVER_PASSWORD` 等同取得 host-root 等級的維運能力，只准平台管理員
> 使用，密碼必須放在部署 secret，不能與一般帳號共用。啟用後也要把這個權限事實納入
> 存取盤點與離職回收流程。

> **首次建置的入口卡片**：`AUTO_REGISTER_LINKS` 已移除，fresh DB 的首頁不會再自動
> 長出入口卡片；既有資料不會被這次收斂刪除。平台管理員需在 `/platform-links` 手動
> 建立交付需要的初始入口，建議清單為：`/anila`、`/anilalm`、`/codeserver`、
> `/n8n`、`/gitlab`，以及內網 MLOps 入口 `https://aiops.ai.ncsist.org.tw:4443/`。
> ⚠ `/anilalm/` 現回 **503「尚未開放」是刻意的發行閘，不是故障**——重開程序見 `anilalm-release-gate.md`。

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
bash infra/deployment/intranet/pack-chunks.sh /home/aia/c1147259/project/Huggingface/gemma-4-31B-it /data/staging/chunks
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
| **R1** | `build-and-export-for-intranet.sh` 出 01–03 tar.gz + INTRANET-LOAD.sh | bundle 以 `COMPOSE_PROJECT_NAME=anila-restart` 產 tag；loader 只做 checksum 驗證與 `docker load`，不 re-tag。起棧要沿用相同 `-p`，並疊 image override。 |
| **R2** | 空機模擬:停 dev stack → INTRANET-LOAD.sh(sha256+load)→ `ANILA_HF_DIR=<staging>/rehearsal-hf` 起 models stack + `deploy-prod.sh` → §3 驗收清單 → 還原 dev stack(用原本 annotated-tag 工作樹重 build,不 checkout branch) | ⚠ 需重開機修 NVIDIA driver mismatch(host NVML 掛了,新 GPU 容器起不來);維護時段 user 排 |

```bash
# R0 (背景跑,log 看進度):
bash /home/aia/c1147259/intranet-staging/rehearsal-r0.sh \
  > /home/aia/c1147259/intranet-staging/rehearsal-r0.log 2>&1 &
# 產出: intranet-staging/chunks/(上傳 Drive 的轉移物)
#       intranet-staging/rehearsal-hf/(R2 的 ANILA_HF_DIR,演練完可刪)
```

> R2 過了,同一套 chunks + bundle 直接上傳 Drive — 演練品即交付品
> (平台 01–03 例外:codex 複核後從乾淨 commit 重 build 重打包)。

### 1.3 Secret 生成 (建議到內網主機上跑)

```bash
SECRET_KEY="$(openssl rand -hex 32)"
echo "SECRET_KEY=$SECRET_KEY"
echo "CSP_SECRET_KEY=$SECRET_KEY"                    # legacy alias,必須與 SECRET_KEY 同值
echo "CSP_SERVICE_TOKEN=$(openssl rand -hex 32)"   # 平台內部 s2s（若 compose 仍要求）；≠ agent 派工身分
echo "ASR_DECODER_TOKEN=$(openssl rand -hex 32)"    # asr-gateway ↔ asr-decoder 的共享密鑰
echo "INTERNAL_PLATFORM_API_KEY=sk-internal-$(openssl rand -hex 24)"
echo "ADMIN_PASSWORD=$(openssl rand -base64 24)"
echo "CSP_DB_PASSWORD=$(openssl rand -hex 32)"
echo "CSP_APP_DB_PASSWORD=$(openssl rand -hex 32)"
echo "CODESERVER_PASSWORD=$(openssl rand -base64 24)"
```

輸出直接進密碼管理器。**不要沿用試用機 .env 的值。**
(DB 兩把用 hex 是必要的 — 會嵌進 DATABASE_URL,避免特殊字元。)

> ⚠ **P2.1（2026-08-01）**：agent 派工改短效 JWT，**不要**再為所級 agent 核發／貼上
> `csk-`／`CSP_SERVICE_TOKEN`。上列 `CSP_SERVICE_TOKEN` 若仍出現在平台 compose，屬
> Router／gateway 等**平台內部**憑證，與 agent 上手無關（見 `docs/guides/developer-guide.md`）。

---

## 2. Phase 1:內網首次部署

### 2.1 帶進內網的東西

1. 與 image bundle 對應的 ANILA repo 內容(由 `restart/from-redesign` 的 annotated tag
   export;`MANIFEST.txt` 記錄 tag+commit)。內網端不 checkout branch,直接接收 bundle。
2. `/tmp/anila-images-export/` 整個資料夾
3. `server.pfx` (wildcard 憑證+key;在 My-OpenAI-Frontend repo 的 `nginx/cert/`,內網 .12 上也有同一份)
4. 8 個 secret 值 (密碼管理器；`SECRET_KEY` / `CSP_SECRET_KEY` 是同值別名)

### 2.2 內網主機 (.15) 初始化

```bash
cd /opt/anila   # repo 解壓處

# 1. TLS:從 pfx 抽 wildcard 憑證+私鑰 (pfx 密碼為空,直接 Enter / -passin pass:)
openssl pkcs12 -in /path/to/server.pfx -clcerts -nokeys -legacy -passin pass: \
  | openssl x509 > infra/nginx/certs/server.crt
openssl pkcs12 -in /path/to/server.pfx -nocerts -noenc -legacy -passin pass: \
  | openssl pkey > infra/nginx/certs/server.key
chmod 600 infra/nginx/certs/server.key
# 驗:subject 應為 CN=*.ai.ncsist.org.tw
openssl x509 -in infra/nginx/certs/server.crt -noout -subject -dates

# 2. CA:預設用 repo 內 CSPKI bundle(內網模型 https 與卡片登入「同一套」CSPKI CA)。
#    intranet-deploy.sh [2/7] 會自動 cp;手動等同下行(不必再下載 NCSISTCA):
mkdir -p share/pki
cp services/csp/app/services/cspki_ca_bundle.pem share/pki/model-ca.pem
# 驗:host 端先確認信任鏈成立(verify return code: 0)再交給容器
echo | openssl s_client -connect 10.53.100.12:443 -servername aiagent2.ai.ncsist.org.tw \
  -CAfile share/pki/model-ca.pem 2>/dev/null | grep 'verify return code'

# 3. .env
cp .env.example .env
nano .env   # 填 8 個 secret 值 + MODEL_GATEWAY_API_KEY + CARD_INITIAL_OWNERS
            # 並打開 ANILA_MODEL_CA_FILE=/etc/anila/pki/model-ca.pem

# 4. import image (內含 sha256 驗檔 + docker load;不 re-tag)
cd /tmp/anila-images-export && bash INTRANET-LOAD.sh
cp /tmp/anila-images-export/intranet-image-overrides.yml /opt/anila/intranet-image-overrides.yml
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
ANILA_ALLOW_GRPC_ENDPOINT=0       # 只有要接 Triton gRPC embedder 才設 1,見 §3.1c
ANILA_TRUSTED_HOSTS=aiagent2.ai.ncsist.org.tw   # FQDN 解到私網 IP,要點名放行

ANILA_HOST=anila.ai.ncsist.org.tw
# 入向 Host 白名單(≠ 上面那條出向 SSRF)。不填就用這個值,見 §3.1d;
# 換 IP／FQDN 要跟 nginx 的 $is_anila_host map 一起改,鎖住自己時設 * 自救。
ALLOWED_HOSTS=localhost,127.0.0.1,csp,10.53.100.15,172.16.120.35,*.ncsist.org.tw
ANILA_AUTH_MODE=card-only
CARD_INITIAL_OWNERS=1147259       # 你的員工編號;加同事用 CSV

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
# 🔴 防呆:任何角括號佔位還沒填,source 會在那一行中止、印一兩行紅字、
# 但整條「回傳成功」(rc=0),從那一行之後的所有鍵都沒載進環境——
# 而 preflight 會拿空 key 去探測 gateway。先掃一次:
if /usr/bin/grep -q '^[^#]*<' .env; then
  echo "🔴 .env 還有未填的角括號佔位(source 會在中途靜默斷掉):" >&2; /usr/bin/grep -n '<' .env; exit 1
fi
# 🔴 操作紀律**（2026-08-22 活體事故換的，隔離重走沒隔離成）**：
#   任何 `cd <dir>` 若失敗，後續命令會**在原目錄照跑**、踩到錯的檔案。
#   破壞性命令一律 `cd <dir> && <cmd>` 串接，**不准 `cd` 眼 `rm/cp/寫入` 分兩行**。
set -a; source .env; set +a
bash infra/deployment/scripts/deploy-prod.sh preflight   # 遠端模型模式:自動建 anila-models-net
                                        # + curl 探測 gateway (帶 Bearer key)
docker compose -p anila-restart \
  -f compose.yaml -f infra/compose/asr-cpu.yml -f intranet-image-overrides.yml \
  --profile asr up -d --no-build  # image 已 load,跳過 build; INCLUDE_ASR=0 時移除 --profile asr
# .15 是 CPU 主機；GPU 主機把 infra/compose/asr-cpu.yml 換成 infra/compose/asr-gpu.yml。
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

### 3.1c 接 Triton / KServe gRPC embedder (protocol=triton_grpc)

> **模型全走 aiagent2 的 OpenAI 相容 https 端點者跳過本節。**
> 只有要把 embedding 直接指到 Triton Inference Server 的 gRPC 埠(預設 9001)時才做。
> 這條路徑之所以存在:Triton 的 embedding model 把「查詢」與「文件」放在**不同輸入張量**
> (`query` vs `documents`),走 OpenAI `/v1/embeddings` 沒有辦法表達這個差別 —— 全部
> 被當文件編碼,檢索排序會**無聲**變差(不會報錯、不會有 log)。

**端點填 IP 字面值(Triton 的常態)→ 下面四件都要做;端點填 FQDN → 第 2 件
不用做,共三件。`grpc://` 端點要過的是 url_guard 的兩關 —— scheme 一關、
主機/IP 一關 —— 少哪一件,400 的 `reason` 就不一樣(下面排錯表有對照):**

1. `.env` 設 `ANILA_ALLOW_GRPC_ENDPOINT=1`(過 **scheme** 關)
   —— 只有 cleartext `grpc://` 需要;`grpcs://`(TLS)不需要,維持 0 即可。
   這是 http 旗標的**姊妹分支**,開它不會放寬任何 `http://` 端點;
   loopback / link-local / multicast / cloud metadata 對 `grpc://` 一樣永遠擋。
2. `.env` 設 `ANILA_ALLOW_PRIVATE_ENDPOINT=1`(過 **主機/IP** 關)
   —— **只有端點填 IP 字面值時才要做這一件**(用 FQDN 就跳過),而 Triton 通常就是填 IP(例
   `grpc://172.16.120.35:9001`,10/8、172.16/12、192.168/16 都算私網)。
   ⚠ **把那個 IP 加進 trusted-hosts 沒有用。** trusted-hosts 只繞得過
   「主機名的 DNS 解析結果落在私網」;IP 字面值是先判私網、根本不看 trusted。
   實測(2026-08-03,本樹 `anila_core.security.url_guard`):
   ```
   grpc 旗標=1,grpc://172.16.120.35:9001                    → 400 reason=private_ip
   grpc 旗標=1 + ANILA_TRUSTED_HOSTS 加 172.16.120.35        → 400 reason=private_ip(沒變)
   grpc 旗標=1 + ANILA_ALLOW_PRIVATE_ENDPOINT=1              → 通過
   grpc 旗標=1 + 端點改 FQDN + 該 FQDN 進 trusted-hosts       → 通過
   ```
   (最後一列在開發機是把 resolver 固定成該 IP 量的 —— 開發機解不到內網 FQDN,
   guard 的判斷邏輯沒有動。)
   兩條路二選一:開私網旗標(簡單,但整段 RFC1918 都放行),
   或端點改用 FQDN 並把該 FQDN 加進 trusted-hosts(較窄,但要有內網 DNS)。
3. **`up -d csp`,不是 `docker restart csp`;而且 `up -d csp` 之後要 reload nginx**
   —— `restart` 不重載 `.env`。旗標沒進容器的症狀與旗標沒設**完全一樣**,
   確認方式:`docker exec <csp 容器> printenv ANILA_ALLOW_GRPC_ENDPOINT`,
   **沒有輸出就是沒進去**(私網旗標同理)。
   ⚠ recreate 過的容器會換 IP,而 nginx 的 upstream 區塊只在載入設定時解析一次
   → **全站 502,但 `docker compose ps` 每個容器都是綠的**,從容器狀態完全看不
   出來。`deploy-prod.sh` 的 `up` / `restart` 路徑已經內建這一步(`reload_nginx`),
   但手動只 recreate 一個服務時沒有人幫你做:
   ```bash
   docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp
   docker exec anila-nginx nginx -t && docker exec anila-nginx nginx -s reload
   ```
4. 模型頁註冊:protocol 選「Triton/KServe gRPC」,端點填 `grpc://host:9001`
   (**不要加 `/v1` 路徑**,gRPC 沒有路徑),模型名稱要與 Triton 上的 model name 一字不差。
   Triton 不吃 Bearer 金鑰,所以該協定下表單**不顯示**金鑰欄位。

> **重跑 `intranet-deploy.sh` 不會把這兩個旗標改回 0。** 腳本對
> `ANILA_ALLOW_GRPC_ENDPOINT` / `ANILA_ALLOW_PRIVATE_ENDPOINT` /
> `ANILA_ALLOW_HTTP_ENDPOINT` 一律「缺鍵才補 0,已有值就一個字都不動」,並在值
> 為 1 時印 warn。以前是每次硬寫 0 —— 操作者照本節開好、隔天重跑一次部署腳本,
> Triton embedder 就靜默失效,而症狀只是 400,現場幾乎反推不出原因。
> 「已有值」的判準跟 docker compose 一致(實測 v2.36.2):行首空白、`export`
> 前綴、`=` 前後空白、單/雙引號、行尾空白、CRLF、行尾註解,以及**引號加行尾
> 註解**(`ANILA_ALLOW_GRPC_ENDPOINT="1"  # 為了 Triton`)都算已設。以前只認
> `^KEY=`,所以手寫成 ` ANILA_ALLOW_GRPC_ENDPOINT=1`(前面多一個空格)時腳本
> 看不見那一行,會在檔尾再 append 一行 `=0`,compose 取最後一筆 → 旗標被靜默
> 關掉。(「引號 + 註解」那一種 2026-08-05 才補上:值有被保留、只是**沒有印出
> warn**,所以部署輸出不會提醒你這台機器帶著放寬的旗標在跑。)

```bash
# 1. 兩個旗標真的進到容器(沒輸出 = 沒進去,回頭做第 3 步)
docker exec anila-restart-csp-1 printenv ANILA_ALLOW_GRPC_ENDPOINT
docker exec anila-restart-csp-1 printenv ANILA_ALLOW_PRIVATE_ENDPOINT

# 2. 端點在網路上通(csp 容器沒裝 curl / grpcurl,用 python socket)
docker exec anila-restart-csp-1 python3 -c \
  "import socket;s=socket.create_connection(('172.16.120.35',9001),3);print('tcp ok');s.close()"

# 3. 註冊後:模型頁該列應為 online;取一段文字經 /v1/embeddings 應回 4096 維
#    (Content-Type 要是 application/json —— SPA catch-all 會回 200 text/html)

# 4. 查詢/文件真的走不同張量(這條路徑存在的理由,也是 url_guard 的活體驗收):
#    同一段文字送兩次、一次 query 一次 document,cosine 必須明顯小於 1.0。
#    等於 1.0 = 查詢被當文件編碼了,不會報錯、排序無聲變差。
docker exec anila-restart-csp-1 python3 -c "
from anila_core.security.url_guard import validate_outbound_url
from app.services.triton_grpc import client as tc
URL, MODEL = 'grpc://172.16.120.35:9001', 'nv-embed-v2'
validate_outbound_url(URL, 'model')            # 旗標不對這行就先炸
print('health', tc.probe_triton_health(URL, model_name=MODEL))
q = tc.embed_texts(URL, MODEL, ['找出去年的採購紀錄'], role='query')[0]
d = tc.embed_texts(URL, MODEL, ['找出去年的採購紀錄'], role='document')[0]
cos = sum(a*b for a,b in zip(q,d)) / ((sum(a*a for a in q)**.5)*(sum(b*b for b in d)**.5))
print('dim', len(q), 'cosine(query,document)', round(cos,4))
"
# 2026-08-03 在本開發機對 172.16.120.35:9001 實測:health ('healthy', <ms>)、
# dim 4096、cosine 0.7467。
# ⚠ probe_triton_health 回的第二個值是**那一次的延遲毫秒數**,不是期望值 ——
#   當天量到 4,下一次是別的數字都正常。要對得上的是 'healthy'、4096,以及
#   cosine 明顯小於 1.0(當天 0.7467;不同權重/文字會不同,重點是 ≠ 1.0)。
```

**排錯**
| 症狀 | 原因 |
|---|---|
| 註冊 400,detail 提到 `scheme` | grpc 旗標沒設,或設了但沒 `up -d`(見第 3 步) |
| 註冊 400,detail 提到私網 / `reason=private_ip` | 端點是私網 IP 字面值而 `ANILA_ALLOW_PRIVATE_ENDPOINT` 沒開(見第 2 步)。**加 trusted-host 治不了這個** |
| 註冊 422「必須為 grpc:// 或 grpcs://」 | protocol 選了 triton_grpc 卻填 http URL |
| 健檢 unhealthy、但 TCP 通 | Triton 上沒載入這個 model name(`ModelReady` 說了算,不會用 ServerLive 漂綠) |
| 502「模型服務暫時不可用」,csp log 是「triton 未在 30s 內回應 ModelInfer」 | **單筆**逾時 —— 上游過慢或該 model 沒載入。單次請求的執行緒佔用上限 35 秒(`_wait_ready` 5s + ModelInfer 30s),重試 3 次 |
| 502,csp log 是「triton call exceeded its 35s budget … 請縮小批次」 | **整批**吃光了整通呼叫的 35 秒預算(每段文字各一次 ModelInfer)—— 縮小 `EMBEDDING_BATCH_SIZE`(見下方「縮小批次」),或調高 `EMBEDDING_TIMEOUT`(見下方「調高 EMBEDDING_TIMEOUT」) |
| 整批帶入(bulk import)報 422 | Triton 沒有 OpenAI `/v1/models` 列表,不支援整批帶入 —— 逐一註冊 |

**縮小批次(`EMBEDDING_BATCH_SIZE`,ingestion-worker 側)**

上面那句「縮小批次」在 2026-08-07 以前是**做不到的動作**:ingestion-worker
把一份文件的所有 chunk 塞進單一 `/v1/embeddings` 請求,沒有任何旋鈕可縮,
所以文件一大就整份失敗,跟表格內容無關。現在它會切成每次最多
`EMBEDDING_BATCH_SIZE` 段(預設 32),每個請求各自拿到完整的 35 秒預算。

要守的不變式是這一條 —— 注意它有**兩個鐘**,而**小的那個說了算**:

```
EMBEDDING_BATCH_SIZE × 每段文字的推論延遲
        < min( EMBEDDING_TIMEOUT_SECONDS ,  EMBEDDING_TIMEOUT )
                 ↑ ingestion-worker 的        ↑ csp 的(另外還要
                   httpx 逾時,預設 30           扣掉 5s channel-ready,
                                                所以整通預算 5+30=35)
```

**預設姿態下是 worker 那個鐘先響**(30 < 35)。兩個變數在**不同容器**裡,
只調其中一個不會改變什麼;兩個都已接進 compose(見下方兩段的第 3 步)。

**每段延遲要用哪個數字**:樹裡唯一的數字是 `triton_grpc/client.py` 模組
docstring 的「measured per-text latency is ~0.02s」。⚠ **那是該檔案的說法,
不是本 runbook 量出來的**;上線前請在自己的機器上量一次(§3.1c 第 4 步的
`embed_texts` 片段跑 N 段計時即可)。以 0.02 秒代入:

| | 依 0.02 秒/段推算 |
|---|---|
| 預設 32 段用掉的時間 | 0.64 秒 / 30 秒 |
| 開始吃緊的每段延遲 | 約 0.9 秒(32 × 0.9 = 28.8) |
| **改版前**單一請求撐得住的段數 | 約 **1500 段**(worker 30 秒)/ 1750 段(csp 35 秒) |

最後一列是**判斷舊故障是不是這個 bug** 的依據:每段 0.02 秒時,舊碼要到
~1500 段才會整份失敗。`ingestion-worker/main.py:21` 引用的是「5k chunks」的
文件,所以真實文件確實會越過它;但若你的文件遠小於 1500 段而仍然失敗,
那**不是**這個 bug,請往表格上面幾列找。段數越多、或每段延遲比 0.02 秒高,
門檻越低 —— 例如每段 0.2 秒時,150 段就會炸。

```bash
# 1. .env 改值(沒有這個鍵就自己加一行;compose 預設 32)
grep -nE '^[[:space:]]*(export[[:space:]]+)?EMBEDDING_BATCH_SIZE[[:space:]]*=' .env

# 2. 套用:一定是 up -d(recreate),docker restart 不重載 .env
docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d ingestion-worker

# 3. 確認它真的到了容器裡(這一步不能跳)
docker exec anila-restart-ingestion-worker-1 printenv EMBEDDING_BATCH_SIZE
```

> ⚠ 這個變數要有 `infra/compose/platform.yml` 的 **ingestion-worker** 區塊裡
> 那一行 `EMBEDDING_BATCH_SIZE: "${EMBEDDING_BATCH_SIZE:-32}"` 才會進到容器
> (compose 沒有 `env_file:`)。缺那一行,`.env` 怎麼改都沒有作用,而且沒有
> 任何錯誤訊息。上面第 3 步就是用來看穿這件事的。

**調 worker 這一側的逾時(`EMBEDDING_TIMEOUT_SECONDS`)**

不變式的另一項,2026-08-07 才接進 worker 容器(在那之前 `.env` 怎麼設都到不了)。

```bash
grep -nE '^[[:space:]]*(export[[:space:]]+)?EMBEDDING_TIMEOUT_SECONDS[[:space:]]*=' .env
docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d ingestion-worker
docker exec anila-restart-ingestion-worker-1 printenv EMBEDDING_TIMEOUT_SECONDS
```

> ⚠ 它跟 csp 的 `EMBEDDING_TIMEOUT` **不是同一個變數**,也不在同一個容器。
> 預設 30 秒短於 csp 單次呼叫的 35 秒預算,更短於 csp 連重試 3 次的 ~106.5 秒
> (`3 × 35 + 0.5 + 1.0`)。所以 **worker 會在 csp 還在重試時就放棄**。後果有兩半,
> 證據強度不同,請分開看:
>
> - **算力一定白花(讀碼可證)**:csp 的 `asyncio.to_thread` 取消不掉,那一批會被
>   做完,而向量沒有人收得到。
> - **會不會被記帳:本包未驗證**。`enqueue_usage` 寫在那個 `await` 之後
>   (`proxy/service.py:291`),所以 handler 若因客戶端斷線被 ASGI server 取消,
>   它就不會執行(＝不記帳);沒被取消就會記。是哪一種要活體測才知道,本包沒測。
>   上面那句按「成本可能較高」的方向提醒 —— **那是刻意保守的假設,不是量到的
>   結論**,跟本節其他數字不同級。
>
> 要讓 csp 的重試真的幫得上忙,這個值得大於 ~106.5 —— 本包**沒有**改預設值,
> 只是把旋鈕接進容器並把關係寫清楚,改不改是運維決定。

**帳單怎麼算(成功與失敗不一樣,請分開看)**

- **成功的文件:金額不變。** csp 逐段文字計價(`prompt_tokens = sum(...)`),
  同樣的內容切成幾批,`token_usage` 的總和一樣。實測 20 段文件,批次 4 與
  不切批同為 60 tokens。調 `EMBEDDING_BATCH_SIZE` 只改變 HTTP 請求次數與
  `token_usage` 的**列數**,不改變總額。
- **失敗的嘗試:會計到錢,而改版前是 0。** csp 只在成功時 `enqueue_usage`
  (`proxy/service.py:291`),所以舊碼一份失敗的文件整趟計 0;現在失敗批次
  **之前**那幾批已經各自成功、各自記過帳。而 arq `max_tries=3` 會把整個 job
  重跑,那幾批**每次重試都重算一遍**。實測(20 段,乾淨通過 = 60 tokens):

  | 情境 | 改版後 | 改版前 |
  |---|---|---|
  | 中段永久失敗(重試 3 次) | 72 | 0 |
  | 最後一批永久失敗 | 144 | 0 |
  | 中段失敗、第 3 次成功 | 108(1.80×) | 60 |
  | 最後一批失敗、第 3 次成功 | 156(2.60×) | 60 |

  上限**嚴格小於一次乾淨通過的 3 倍**(失敗那一批本身永遠不計帳,而
  `max_tries=3`)。縮小批次**不會**降低這個上限,只讓已付的粒度變細。
  ⚠ 舊版的那個 0 不是折扣:上游其實已經逐段推論到失敗點才停(`client.py:354`
  是逐段迴圈),那些算力當時**做了卻沒有人被計費**。新行為計的是真的做過的工。

**某一批失敗會怎樣**:整份文件失敗,不會半份入索引。csp log / 文件的
`error_message` 會指出是第幾批、對應原文的哪一段範圍(例:「第 7/12 批失敗,
對應第 192–223 段文字」)。**失敗那批之後的批次不會送出**,所以不會為一份
已經注定失敗的文件繼續花 token。刻意不做「續傳」:入索引是全份一次寫入,
而 arq `max_tries=3` 會把記住的進度重新 embed 一次 —— 那是重複計費,不是省事。

**調高 `EMBEDDING_TIMEOUT`**

```bash
# 1. .env 改值(沒有這個鍵就自己加一行;compose 預設 30)
#    這裡用 grep 先看現況,再自己編輯 —— 不用 sed,避免改到別的鍵。
grep -nE '^[[:space:]]*(export[[:space:]]+)?EMBEDDING_TIMEOUT[[:space:]]*=' .env

# 2. 套用:一定是 up -d(recreate),docker restart 不重載 .env
docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp

# 3. recreate 過就要 reload nginx,否則上游 IP 是舊的 → 全站 502 但容器全綠
docker exec anila-nginx nginx -t && docker exec anila-nginx nginx -s reload

# 4. 確認它真的到了容器裡(這一步不能跳)
docker exec anila-restart-csp-1 printenv EMBEDDING_TIMEOUT   # 應印出你設的值
```

它同時是整通呼叫的預算主項:budget = `_wait_ready` 5s + `EMBEDDING_TIMEOUT`,
與批次大小無關;調到 60,單次請求的執行緒佔用上限就從 35 秒變成 65 秒,
一個 HTTP 請求最久 `3 × 65 + 0.5 + 1.0` ≈ 196.5 秒(重試 3 次)。調之前先確認上游真的
只是慢,而不是 model 沒載入 —— 後者調多久都不會好。

> ⚠ 這個變數要有 `infra/compose/platform.yml` 的 csp 區塊裡那一行
> `EMBEDDING_TIMEOUT: "${EMBEDDING_TIMEOUT:-30}"`(v-2026-08-03 起有)才會進到
> 容器。compose **沒有 `env_file:`**,`.env` 只是變數來源,不會整包灌進容器 ——
> 缺那一行的版本,`.env` 怎麼改都沒有作用,而且沒有任何錯誤訊息:`printenv` 是
> 空的、行為一模一樣。上面那條 `printenv` 就是用來看穿這件事的。

### 3.1d 入向 Host 白名單 `ALLOWED_HOSTS`(換 IP／換 FQDN 時會踩到)

csp 的 `TrustedHostMiddleware`:Host header 不在名單內 → `400 Invalid host header`,
請求碰不到任何路由。它是 08-06 CSRF 修補之後的第二層,擋的是「Host 裡夾路徑」
那類偽造;因為註冊在最外層,偽造的 Host 在被其他中間層讀到之前就死了。

⚠ 跟 `ANILA_TRUSTED_HOSTS` **是兩回事**:那條是**出向** SSRF 白名單(csp 可以打誰),
這條是**入向**(誰可以打 csp)。名字像,救不了對方。

```bash
# 預設值(.env 不寫這行也是這個值,compose 端已內建):
ALLOWED_HOSTS=localhost,127.0.0.1,csp,10.53.100.15,172.16.120.35,*.ncsist.org.tw
```

- **不帶 port**。比對只看 `host.split(":")[0]`,寫成 `csp:8000` 永遠不會命中。
- `localhost` / `127.0.0.1` / `csp` **由程式強制併入**,從 `.env` 刪掉也刪不掉。
  它們不是政策而是這套部署的結構:csp 自己的 healthcheck 打 `localhost:8000`、
  nginx 的 loopback 探測轉發 `Host: 127.0.0.1`、router／studio／asr-gateway／
  ingestion-worker 一律走 `http://csp:8000`。少了任何一個,`depends_on:
  csp: service_healthy` 會讓 nginx 根本起不來。
- **這份跟 `infra/nginx/anila.conf` 檔頂的 `map $host $is_anila_host` 是同一組意圖的
  兩份手抄本（集合刻意不相等：csp 這邊多 `csp`、少 `10.53.100.12`，見下）
  ——改一邊就要重新推導另一邊。** 只改 nginx:Host 進得了 nginx 但被 csp 擋 → 瀏覽器
  看到 400,而 `docker ps` 全綠。只改這邊:nginx 先回 444,csp 這條沒機會生效。
- csp 這邊**沒有** `10.53.100.12`(nginx 有)。那是模型主機,沒有任何呼叫方會用它
  當 Host 打 csp。真的要從那個位址進來,兩邊都要加。

換 prod IP / DNS 上線改用 FQDN 的動作(跟 §3.1c 同一套姿勢):

```bash
# 1. .env 改 ALLOWED_HOSTS(以及 ANILA_HOST),同步改 nginx 那條 map
# 2. 套用:一定是 up -d(recreate);docker restart 不重載 .env
docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp
# 3. recreate 過要 reload nginx,否則上游 IP 是舊的 → 全站 502 但容器全綠
docker exec anila-nginx nginx -t && docker exec anila-nginx nginx -s reload
# 4. 確認名單真的到了容器裡,而且檢查真的開著
docker exec anila-restart-csp-1 printenv ALLOWED_HOSTS
docker compose -p anila-restart logs csp 2>&1 | grep 'host allow-list:'
# 5. 驗行為(csp 容器沒裝 curl,用 python;在 host 上驗 nginx 那一段用 curl -k)
curl -sk -o /dev/null -w '%{http_code}\n' https://<你的FQDN>/health      # 200
curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' \
     https://10.53.100.15/health                                          # 444(nginx 先擋)
```

第 4 步那條 grep **一定會有輸出**,兩種狀態各印一行(2026-08-06 對真 uvicorn
開機實測逐字):

```
csp - INFO - host allow-list: ENFORCED — 6 host(s): localhost, 127.0.0.1, csp, 10.53.100.15, 172.16.120.35, *.ncsist.org.tw
csp - WARNING - host allow-list: DISABLED — ALLOWED_HOSTS is '*', every incoming Host header is accepted
```

- `ENFORCED` = 開著,而且後面**列出實際生效的名單**(含程式併入的那三個)——
  要對的是這一行,不是 `.env` 裡寫了什麼。
- `DISABLED` = 沒開(`*`)。這是 WARNING,不是 INFO。
- **一行都沒有 = 開機沒走到那一步**(第三種狀態,不是「沒開」)。往上翻
  startup_security / alembic 的錯誤,見 §3.2。
- ⚠ 這行在 **lifespan** 印,不在 import 期 —— import 期 `setup_logging()` 還沒跑,
  root logger 沒有 handler,那時候印什麼都會被丟掉。舊版就是這樣,grep 永遠是空的。

`Host` 比對**不分大小寫、忽略結尾的那個點**(RFC:主機名大小寫不敏感、DNS 根
標籤可省)。`ANILA.AI.NCSIST.ORG.TW`、`anila.ai.ncsist.org.tw.` 與名單上的
`*.ncsist.org.tw` 是同一個名字,三者同樣放行;`ncsist.org.tw.evil.com` 不是。

#### 症狀對照表

| 看到的 | 多半是 | 去哪裡 |
|---|---|---|
| 容器全綠,瀏覽器 `400 Invalid host header` | Host 過得了 nginx 但不在 csp 名單 —— 換 IP／FQDN 只改了一邊 | 下面的 🔓 自救 |
| 瀏覽器直接被切線(空回應),csp 完全沒收到 | nginx 的 `$is_anila_host` 先回 444 | 改 `infra/nginx/anila.conf` 檔頂那條 map |
| **csp 起不來**,log 最後一行是 `ALLOWED_HOSTS contains a malformed wildcard pattern: '…'` | 名單裡有壞掉的萬用字元(最常見:想涵蓋整個網段而寫成 `10.53.*.15`) | 照錯誤訊息點名的那個 pattern 改掉;只支援 `*.suffix` 一種形狀 |
| csp healthy 一陣子後轉 unhealthy,nginx 一直起不來 | **不該再看到這個** —— 舊版壞 pattern 會註冊成功、每個請求(含 `/health`)500。現在改成開機就拒絕(上一列) | 若真的看到,先跑 🔓 自救,再回報 |

> 為什麼壞 pattern 要讓 csp 直接起不來:starlette 用 `assert` 檢查 pattern,而
> FastAPI 是**延遲**建立中間層的 —— 註冊當下不會炸,炸在第一個請求。那條路徑的
> 終點是「healthy → unhealthy → `depends_on: csp: service_healthy` → nginx 永遠
> 起不來」,一個 typo 換一次全院停機,而且沒有任何訊息點名它。開機就拒絕、並把
> 那個 pattern 印出來,是這棵樹處理壞設定的既有姿勢(見 §3.2 startup_security)。

#### 🔓 把自己鎖在外面了怎麼自救

```bash
# 立刻恢復:把檢查整個關掉(* = 停用),平台馬上回來
#   在 .env 把 ALLOWED_HOSTS 改成一顆星,然後 recreate(不是 restart):
docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp
docker exec anila-nginx nginx -t && docker exec anila-nginx nginx -s reload
# 確認:這一行要從 ENFORCED 變成 DISABLED(不是「變成沒有」)
docker compose -p anila-restart logs csp 2>&1 | grep 'host allow-list:'
```

平台回來之後再查該補哪個 Host:從 csp 的 access log 找那個被擋的 Host,加進
`ALLOWED_HOSTS`(以及 nginx 的 map),再把 `*` 換回名單。**先恢復服務,再查原因**
—— 這條開關的存在就是為了不用在停機狀態下除錯。

> 🔧 長期照顧:這份名單在 repo 裡有兩個真來源 —— `infra/compose/platform.yml`
> 的 csp 區塊(csp 用的)與 `infra/nginx/anila.conf` 檔頂的 `map $is_anila_host`
> (nginx 用的)。`.env.example` 與本檔的兩處抄本已由
> `services/csp/tests/test_allowed_hosts_middleware.py` 自動比對,漂開會紅;
> **compose ⇄ nginx map 這一對仍是手工同步的**,沒有測試看著。改任何一邊都要
> 想到另一邊,兩者刻意差兩項(csp 多 `csp`、少 `10.53.100.12`,理由見 compose 註解)。

### 3.2 startup_security 一定要過

```bash
docker compose -p anila-restart logs csp 2>&1 | grep -E "startup_security|RuntimeError|Refusing"
```

| 看到的 log | 處理 |
|---|---|
| 沒輸出 | 正常,繼續 |
| `Refusing to start: ... dev 預設值: SECRET_KEY/ADMIN_PASSWORD/DB_PASSWORD...` | 對應 secret 沒換真值 (§1.3 重生) |
| `ANILA_AUTH_MODE` 不是 `password`、`mixed` 或 `card-only` | 單一登入模式值錯誤 |
| compose 階段 `required variable XXX is missing` | .env 漏填,csp 根本沒起 |

### 3.3 健康檢查 + 模型鏈路

```bash
curl -k https://localhost/health                       # 200
docker compose -p anila-restart ps                                       # 全部 healthy
# 模型 e2e (在 host,key 換真值):
curl --cacert share/pki/model-ca.pem \
  -H "Authorization: Bearer $MODEL_GATEWAY_API_KEY" \
  https://aiagent2.ai.ncsist.org.tw/v1/models           # 應列出 openai/gpt-oss-20b 等
# csp 容器內 DNS 解析確認:
docker compose -p anila-restart exec csp python -c "import socket; print(socket.gethostbyname('aiagent2.ai.ncsist.org.tw'))"
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
cd /opt/anila && docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp
# /models 應出現 image-generator (圖像繪製);對話輸入「畫一張…」驗證 dispatch
```

---

## 5. Phase 4:後續維運

### 5.1 解凍 codeserver / n8n / gitlab

nginx 對 `/codeserver` `/n8n` `/gitlab/` 預設 `return 404`。解凍 = 把該 location 的那一行 `return 404;` 刪掉 → `docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d --force-recreate nginx`。
（`infra/nginx/anila.conf` 是**單檔 bind-mount**；git 改檔會換 inode，容器仍抓舊檔且無任何錯誤。`restart`／`reload` 都不夠，見 `anila.conf:447`。）

### 5.2 TLS cert rotation

wildcard 憑證 2029 到期;換發後同 §2.2 步驟 1 重抽,`docker compose -p anila-restart restart nginx`。
NCSIST CA 換代時同步更新 `share/pki/model-ca.pem` 並 `docker compose -p anila-restart restart csp`。
（§5.2 的 restart 沒問題：憑證／CA 是**目錄**掛載，不是單檔 inode 綁定，換成新檔後 restart 就能讀到。）

### 5.3 Postgres backup

權威手順（排程、保留、還原演練）見 [`csp-db-backup-restore.md`](./csp-db-backup-restore.md)。  
一行備忘：`ANILA_DB_CONTAINER=… ANILA_BACKUP_DIR=/var/backups/anila bash infra/deployment/scripts/backup-csp-db.sh`

### 5.4 加 owner / 模型 gateway key 輪替

- 新 owner:改 `CARD_INITIAL_OWNERS` (只對新刷卡者生效) 或 `/users` UI 改既有帳號
- gateway key 輪替:aiagent2 重簽 → 改 `.env` `MODEL_GATEWAY_API_KEY` → `docker compose -p anila-restart -f compose.yaml -f intranet-image-overrides.yml up -d csp`

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

1. **DNS**:`anila.ai.ncsist.org.tw → 10.53.100.15` A record
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
