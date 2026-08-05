# Runbook — 串流語音輸入(ASR)

對話框那顆麥克風按鈕的開關、驗證與故障分診。
架構與 WS 協定見 `services/asr-gateway/README.md`;compose 定義在
`infra/compose/platform.yml`(`asr-gateway` / `asr-decoder`,兩者都在 `asr` profile)。

---

## 0. 先讀這一段:麥克風按鈕是自己決定要不要出現的

前端**沒有**「有沒有語音」的建置旗標。`apps/anila-shell/src/asr/asrStream.js:418`
的 `probeAsrAvailable()` 在載入時打一次 `GET /asr/health`:

| `/asr/health` | 前端行為 |
|---|---|
| **200** | 顯示麥克風按鈕 |
| 其他任何結果(502 / 503 / 連不上) | **不顯示**,而且不會有任何錯誤訊息 |

所以「看不到麥克風」的預設解讀是**語音沒開**,不是壞掉。
這也是 2026-07-31 平台擁有者問「怎麼沒有麥克風」的答案 —— 按鈕的程式碼一直都在,
是 `asr-gateway` 這個容器沒有跑。

⚠ **2026-08-05 起 anilalm 會重探,anila-shell 還不會。**
`apps/anilalm/src/asr/useAsrInput.ts` 改成每 60 秒重探一次(只在分頁看得見時跑,
另外在 WebSocket 出錯時立刻補一次)。原本只在載入時探一次 → 解碼端**開頁之後**
才掛掉會留下一顆「按了就壞」的按鈕;遠端解碼端斷斷續續的機率遠高於本機容器,
所以遠端部署下這條是必要的。
`apps/anila-shell/src/asr/useAsrInput.js` 是同一份邏輯的孿生檔,**這一批沒有動**
(不在本包的檔案範圍內)→ anila-shell 那邊仍然只探一次,兩邊行為目前不一致。

---

## 1. 打開語音

前置:權重要先在本機(見 §3);GPU 直通要能用(見 §4)。

```bash
cd ~/桌面/ANILA/anila-restart-20260729/ANILA

# 產生 gateway ↔ decoder 的共享密鑰,寫進未追蹤的 .env(只要做一次)
# ⚠ .env 已被 .gitignore;這個值絕對不可以進 repo、進 log、進回報。
grep -q '^ASR_DECODER_TOKEN=' .env || \
  printf 'ASR_DECODER_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env

# 起這兩個服務(明列服務名,不要裸跑 up -d 去動到別人正在測的容器)
docker compose -p anila-restart --profile asr up -d --no-recreate asr-decoder asr-gateway

# ⚠⚠ 一定要 reload nginx
docker exec anila-nginx nginx -t && docker exec anila-nginx nginx -s reload
```

**最後那一步不能省。** nginx 的上游位址只在載入設定時解析一次,新起的容器拿到新 IP,
沒 reload 的話 nginx 會一直打舊 IP → **全站 502 但每個容器都顯示 healthy**。
這棵樹已經被這件事咬過三次。`infra/deployment/scripts/deploy-prod.sh` 的
`reload_nginx()`(:281)就是同一個機制。

要讓它變成常駐(這樣裸跑 `docker compose up -d` 也會帶上語音),在 `.env` 加:

```
COMPOSE_PROFILES=asr
```

> ⚠ 沒有 GPU 直通的機器還要加 CPU 覆蓋檔,見 §4。

### 關掉語音

```bash
docker compose -p anila-restart --profile asr stop asr-gateway asr-decoder
docker exec anila-nginx nginx -s reload
```

`stop` 就夠 —— `/asr/health` 變成 502,前端下次載入就自己把按鈕收起來。
要連容器一起清掉用 `rm -f` 取代 `stop`;`.env` 裡的 `COMPOSE_PROFILES=asr` 記得一起拿掉,
否則下次 `up -d` 又會把它叫回來。

**擁有者會看到什麼:** 語音關掉後重新整理頁面,麥克風按鈕自己消失,沒有錯誤、沒有紅字。
打開後重新整理,按鈕自己出現。不需要重建前端、不需要清快取。

---

## 2. `ASR_DECODER_TOKEN` 是什麼

`asr-gateway` 打 `asr-decoder` 的 `POST /transcribe` 時帶的 `X-Token`,兩邊共享的一個亂數。
decoder **沒有任何其他認證** —— 誰連得到那個埠,誰就能拿它免費解碼。

- 內部版(decoder 與平台同機、只掛在 `anila-net`、不開 host port):它是縱深防禦。
- 外部版(decoder 跑在別台 GPU 主機、綁 host port,見
  `services/asr-decoder/docker-compose.standalone.yml`):**它是唯一的一道防線**,
  務必再加防火牆限縮來源 IP。

兩邊都 fail-loud:沒填就拒絕開機(`services/asr-decoder/app/main.py` 的 `_require_token`)。
compose 那邊刻意**不用** `${VAR:?}` —— compose 的變數插值在解析階段就會跑,即使服務被
`profiles:` 擋著不啟動也一樣,用 `:?` 會讓「不想開語音的人連平台都起不來」(2026-07-30 中招過)。

**輪替:** 改 `.env` 的值 → `up -d --force-recreate asr-decoder asr-gateway` → reload nginx。
兩個服務必須一起 recreate,只換一邊會全部 401。

---

## 3. 權重

decoder 的權重**刻意不烘進 image**(換模型尺寸不必重 build,image 也才簽得動)。
由 compose 以唯讀 volume 掛進 `/var/anila/asr-models`,預設來源是
`models/model/asr-models/`(`models/model/*` 已被 `.gitignore`,權重不進 git)。

`ASR_LOCAL_FILES_ONLY=1`(預設)= 禁止任何對 HuggingFace 的出向請求。內網一定要維持 1:
權重缺了要當場 fail loud,而不是卡在 DNS timeout 讓 health 永遠不 ready。

**在有網路的機器上暖快取**(內網請改用離線 bundle):

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD/models/model/asr-models:/var/anila/asr-models" \
  --entrypoint python3 anila/asr-decoder:0.1.0 -c \
  "from faster_whisper.utils import download_model; print(download_model('large-v3', cache_dir='/var/anila/asr-models'))"
```

`ASR_MODEL_DIR` 底下如果直接就有 `model.bin`(離線 bundle 的轉檔目錄),會被當成模型路徑
直接用;否則當成 HuggingFace 快取根目錄,仍以 `ASR_MODEL_SIZE` 解析。同一個掛載點兩種都吃
(`services/asr-decoder/app/model.py` 的 `_resolve_model_ref`)。

---

## 3b. 解碼端在別的機器上(外部 / 算力中心)

擁有者的要求:**「不論是本地還是外部伺服器都要可以連線」——兩條都要留,不是二選一。**
手提氣隙 bundle 帶的是本地 decoder,沒有算力中心的站台只有它;有算力中心的站台可能
連 GPU 都沒有。

### 三個決定

| 決定 | 變數 | 說明 |
|---|---|---|
| 講哪種協定 | `ASR_DECODE_PROTOCOL` | `native`(本地 decoder)/ `openai`(`/v1/audio/transcriptions`)。**填錯的值會讓 asr-gateway 開不了機** —— 這是刻意的,選錯協定的症狀是每句話 404/401 而麥克風看起來正常。 |
| 打哪個位址 | `ASR_DECODE_URL`,或治理中心的 asr-primary | 治理中心指派優先;位址**兩條路都會過 SSRF guard**。 |
| 拿什麼憑證 | 治理中心那筆模型的 `api_key` > `ASR_DECODE_API_KEY`(openai)/ `ASR_DECODER_TOKEN`(native) | 祕密不會出現在 `/asr/health`、log 或錯誤訊息。 |

### 只起 gateway、不起本機 decoder

```bash
# 本地全套(現行指令,沒有變):
docker compose -p anila-restart --profile asr up -d

# 遠端:只起 gateway,解碼交給算力中心
docker compose -p anila-restart --profile asr-remote up -d
docker exec anila-nginx nginx -s reload
```

`asr-decoder` 掛在 `["asr","asr-local"]`,`asr-gateway` 掛在 `["asr","asr-remote"]`,
gateway 的 `depends_on: asr-decoder` 帶 `required: false` → `--profile asr-remote`
只會起 gateway,而且不會去等一個根本不存在的相依。
⚠ **不要再用 `--no-deps`** —— 它會把 `csp` / `redis` 的等待一起跳過,那兩個是真的要等的。

### 驗證

```bash
docker exec anila-restart-asr-gateway-1 \
  python3 -c "import httpx,json;print(json.dumps(httpx.get('http://localhost:8200/asr/health').json(),ensure_ascii=False,indent=2))"
```

看三個欄位:`decode_protocol`(生效的協定)、`decode_url_source`(env / csp_registry /
csp_registry_stale)、`decode_credential_source`(env / csp_registry)。
`reason` 分三種病因:`decoder_unreachable`(連不到)、`decoder_unauthorized`(**金鑰錯**,
不要去查網路)、`decoder_not_ready`(對方在載模型或被限流)。

⚠ csp 容器沒裝 `curl`,用上面的 `python3 -c` 版本;`curl` 回空是假陰性。

---

## 4. GPU:什麼時候要,以及沒有 GPU 時怎麼辦

⚠ **2026-08-05 起 GPU 保留不在 `platform.yml` 裡了**,搬到
`infra/compose/asr-gpu.yml`。原因:平台要搬到**沒有 GPU** 的 CPU 主機
(2× EPYC 9334 / 64 核 / 755 GB),寫死的 nvidia 保留會讓**整批** `up` 失敗,
連不碰語音的服務都起不來。

**GPU 主機要自己疊回去:**

```bash
docker compose -p anila-restart \
  -f compose.yaml -f infra/compose/asr-gpu.yml \
  --profile asr up -d
```

疊上去之後,沒有 nvidia container runtime 的機器 `up` 會直接失敗:

```
could not select device driver "nvidia" with capabilities: [[gpu]]
```

**那是設計,不是 bug。** 悄悄退回 CPU 跑 large-v3 會得到 RTF>1(解碼比說話還慢),
那種「能用但難用到沒人想用」比明著壞更糟。沒疊覆蓋檔時 `ASR_DEVICE` 仍預設 `cuda`,
所以容器會在載模型階段 fail-loud,一樣不會偷偷用 CPU 跑大模型。

> ⚠ **本開發機(2026-07-31)目前就是這個狀態。** `nvidia-smi` 正常、卡是 RTX A4000 16GB,
> 但 `nvidia-container-toolkit` **沒有安裝**(`nvidia-container-cli` / `nvidia-container-runtime`
> 都不存在,`/etc/docker/daemon.json` 裡的 `nvidia` runtime 指向一個不存在的執行檔)。
> 驗證:`docker run --rm --gpus all alpine true` 也一樣失敗 → 這台機器上**任何**容器都拿不到 GPU。
> 修復要 `sudo apt install nvidia-container-toolkit` 並**重啟 docker daemon**,
> 而重啟 daemon 會把整個 stack 打掉 → 請挑沒人在測的時段做,不要順手做。

### CPU 權宜組態

`infra/compose/asr-cpu.yml` 把 GPU 要求拿掉,改用 CPU + 小模型:

```bash
docker compose -p anila-restart \
  -f compose.yaml -f infra/compose/asr-cpu.yml \
  --profile asr up -d --no-recreate asr-decoder asr-gateway
docker exec anila-nginx nginx -s reload
```

本機實測(24 核,4.69 秒語句,`small` + int8,模型已暖機):

| | wall | RTF |
|---|---|---|
| partial(beam=1) | 0.79s | 0.17 |
| final(beam=5) | 0.86s | 0.18 |
| 記憶體 | 1.15 GiB | — |
| GPU | 0 MiB | — |

夠快。**但代價是正確率:** `small` 的繁中正確率明顯不如 `large-v3`,專有名詞與數字最容易錯。
內網 `.15` 上線前務必把 GPU 直通弄好、回到 `large-v3`。

冷啟動模型尺寸對照(同一台、同一段音訊,CPU int8):
`small` RTF 0.26/0.39 可用 · `medium` 0.69/0.86 勉強 · `large-v3` 1.14/1.45 不可用。

---

## 5. 分診:「語音沒開」還是「語音壞了」

這是這份 runbook 最重要的一節。**兩者的表面症狀一模一樣 —— 都是「沒有麥克風按鈕」。**

先跑這一行:

```bash
curl -sk -i https://localhost/asr/health | head -3
```

| 看到什麼 | 意義 | 動作 |
|---|---|---|
| `200` + `Content-Type: application/json` | 語音**開著而且健康** | 沒有麥克風就是前端問題,不是這裡 |
| `502` | 語音**沒開**(或 gateway 沒起來) | 見下面 A |
| `503` + json | gateway 活著,但撤銷清單沒 ready(fail-closed) | 見下面 B |
| `200` + `Content-Type: text/html` | **你打到 SPA catch-all 了,什麼都沒證明** | 網址打錯或 nginx 路由掉了 |

> ⚠ 最後一列是這個 repo 反覆踩到的陷阱:只看 status code 會被 SPA 的 200 騙過去。
> **一定要看 Content-Type。**

### A. 502 —— 先分「沒開」還是「起不來」

```bash
docker ps -a --filter name=anila-restart-asr --format '{{.Names}}\t{{.Status}}'
```

- **列表是空的** → 語音就是**沒開**。這是正常狀態,要開就照 §1。
- **`Exited` / `Restarting`** → 語音**壞了**。看日誌:
  ```bash
  docker logs --tail 50 anila-restart-asr-decoder-1
  docker logs --tail 50 anila-restart-asr-gateway-1
  ```
  常見三種:
  - `ASR_DECODER_TOKEN must be set` → `.env` 沒填,見 §2。
  - `could not select device driver "nvidia"` → GPU 直通沒了,見 §4。
  - decoder 一直重啟且日誌有 `model load failed` → 權重不在或壞了,見 §3。
    (decoder 載不到權重會**故意讓整個 process 以非零碼結束**,好讓 restart policy 重試 ——
    看到重啟迴圈是預期行為,不是二次故障。)
- **兩個都 `Up (healthy)` 但仍然 502** → **十之八九是 nginx 沒 reload。**
  ```bash
  docker exec anila-nginx nginx -s reload
  ```
  再打一次 health。這是這棵樹最常見的假故障:容器全綠、使用者進不來。

### B. 503

gateway 活著,但撤銷清單(revocation cache)還沒同步完,此時所有 WS 一律被拒 ——
health 誠實回 503 而不是騙 operator。通常是 csp 或 redis 剛重啟。
等 10–30 秒;一直不好就查 asr-gateway↔csp 的**平台內部** s2s 設定（若部署仍使用
`CSP_SERVICE_TOKEN`／等效服務客戶端）、以及 csp 與 redis 健不健康。
（此處與 agent 派工 JWT／`csk-` 上手無關。）

### C. 有按鈕、按下去卻失敗

WS 的 close code 就是診斷碼(定義見 `services/asr-gateway/README.md`):

| code | 意義 |
|---|---|
| 4401 | 權杖無效/過期/被撤銷 → 提示重新登入 |
| 4408 | session 逾時(會先 flush,不丟尾句) |
| 4409 | 同一個人開了新連線,舊的被踢 |
| 4503 | 驗證基礎設施不可用(JWKS/撤銷清單),重登也沒用 |

> 🚨 **2026-07-31 已知阻斷:目前一定會收到 4401。** 見 §7。

---

## 6. 隱私:音訊與逐字稿都不落地

擁有者已裁決:**音檔不存、逐字稿不存**,語音純粹是一種輸入法,等同鍵盤。

2026-07-31 對 `services/asr-gateway/app/` 與 `services/asr-decoder/app/` 全樹稽核結果:

- **零檔案寫入** —— 沒有任何 `open()`、`tempfile`、`NamedTemporaryFile`、`aiofiles`、`shutil`。
- **零資料庫** —— 兩個服務都沒有 DB 連線(gateway 只用 Redis,而且只是
  **訂閱**撤銷事件 + 定期對帳,不寫任何東西)。
- **日誌只記 metadata** —— 全部 30 餘處 log 呼叫逐一看過,沒有一處把 `text`
  (逐字稿)或音訊位元組帶進去。decoder 那行刻意只記
  `kind` / `samples` 數量 / `decode_seconds`(`services/asr-decoder/app/main.py:179`),
  上面還留著「辨識內容等同對話內容」的註解。
- **出向目的地只有三個**,全都不含音訊或逐字稿以外的用途:
  decoder 的 `/transcribe`、csp 的 JWKS、csp 的撤銷清單。
- PCM 只在記憶體流轉:`decode_client.py` 把 samples 轉 bytes 後直接進 HTTP body,不落地。

改動這兩個服務時請維持這條線;要加診斷 log,記得**不要**把 `result["text"]` 印出來。

---

## 7. 🚨 已知阻斷(2026-07-31):卡登/帳密權杖都過不了 gateway 的驗章

**症狀:** 麥克風按鈕會正常出現(`/asr/health` 是 200),但一按下去 WS 立刻以
**4401** 關閉,前端提示重新登入 —— 重登也沒用。

**根因:兩個獨立缺陷,都在 `services/asr-gateway/app/auth.py`,不在基礎設施。**

1. **Cookie 名稱對不上。**
   csp 一律簽發名為 `anila_access_token` 的 cookie
   (`services/csp/app/middleware/cookies.py:33`,與 `COOKIE_SECURE` 無關);
   `anila-studio` 也是讀這個名字(`services/anila-studio/app/auth.py:38`)。
   但 asr-gateway 在 `COOKIE_SECURE=true` 時去找 `__Host-anila_access_token`
   (`services/asr-gateway/app/auth.py:36,51`)—— 那個 cookie 從來沒有人簽發過。
   瀏覽器帶著正確的 cookie 過來,gateway **根本看不到權杖**,close reason 是「未登入」。
   ⚠ 把 `COOKIE_SECURE` 改成 false 也救不了:那條路走的是 `anila_dev_access_token`,
   一樣沒人簽發。這不是設定問題,是程式碼問題。

2. **就算讀到權杖,claim 也不夠。**
   gateway 要求 `exp/iat/iss/aud/jti`,外加 session assurance 的
   `jti/sid/amr/acr/auth_time`(`auth.py` 的 `_verify_jwt` 與 `_has_valid_session_assurance`)。
   csp 的 `create_tokens()`(`services/csp/app/services/auth_service.py:55`)只簽
   `sub / username / role / tv / exp / type`。實測驗章錯誤:
   `JWTError: missing required key "aud" among claims`。
   **卡登(`api/auth/card.py:166`)走的是同一個 `create_tokens()`**,所以擁有者用自然人憑證卡
   登入一樣過不了 —— 這不是「帳密登入才有的問題」。
   對照組:`anila-studio` 用 `options={"verify_aud": False}` 且不要求 iss/jti,所以它一直是通的。

**簽章本身是好的** —— gateway 從 csp 的 JWKS 取到 `kid=anila-v1` 的公鑰、驗章通過,
信任錨沒問題。純粹是 claim 契約對不上:gateway 是照一個 csp 還沒實作的 token 格式寫的。

**這要由 app code 的負責人修**(基礎設施這邊沒有旋鈕可以繞過,也不該有 —— 繞過就是弱化 JWT 驗證)。
兩條路二選一:
- 讓 csp 的 `create_tokens()` 補上 `iss/aud/jti/sid/amr/acr/auth_time`(比較正確,但影響全平台);
- 或把 asr-gateway 的要求降到與 anila-studio 一致(比較小,但等於承認 assurance 檢查目前是空的)。

修好之前:語音**不要對擁有者宣布可用**。按得下去但一定失敗,比按鈕不存在更糟。
要暫時收起按鈕就照 §1 的「關掉語音」。

**驗證修好了沒** —— 拿一個真的登入權杖打 WS,看 close code 有沒有離開 4401:

```bash
# 1) 取一個真的 access token(卡登或帳密皆可),存成 token.txt,不要印出來
# 2) 直接問 gateway 它自己怎麼看這個權杖 —— 比猜快得多
docker cp token.txt anila-restart-asr-gateway-1:/tmp/token.txt
docker exec anila-restart-asr-gateway-1 python3 -c "
import asyncio
from jose import jwt
from app.config import settings as s
from app.services import jwks_client
tok=open('/tmp/token.txt').read().strip()
async def m():
    pk=await jwks_client.get_public_key(jwt.get_unverified_header(tok)['kid'])
    p=jwt.decode(tok,pk,algorithms=['RS256'],options={'verify_aud':False})
    need={'exp','iat','iss','aud','jti','sid','amr','acr','auth_time'}
    print('claims:',sorted(p)); print('MISSING:',sorted(need-set(p)))
asyncio.run(m())"
```

`MISSING: []` 才代表缺陷 2 修好了。缺陷 1(cookie 名稱)則要看瀏覽器實際送出的
cookie 名字與 `services/asr-gateway/app/auth.py:36,51` 是否一致。
兩個都修好之後,按下麥克風應該會依序收到
`{"type":"status"}` → `{"type":"partial"}`(數次)→ `{"type":"final","text":...}`。

### 已經驗過的部分(不必重驗)

2026-07-31 在本機實測,**認證以下的每一層都是好的**:

- nginx `/asr/` 路由、TLS、WS upgrade:WS 握手成功(HTTP 101),
  失敗是發生在握手**之後**的應用層 close,不是路由問題。
- gateway → decoder 的網路與 `X-Token` 認證:正確 token 200,錯的/沒帶都 401。
- decoder 真的會辨識:用 `ffmpeg -f lavfi -i flite=text='...'` 合成一段已知英文語句
  (repo 內沒有音訊 fixture,而這台機器沒有 espeak/pico2wave/flite CLI,
  但 ffmpeg 內建 `flite` filter 可以合成),餵 `POST /transcribe` 得到
  逐字一致的辨識結果。

所以 §7 修好之後,語音應該就會直接會通;不需要再回頭懷疑基礎設施。
