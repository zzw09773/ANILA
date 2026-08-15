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

⚠ **2026-08-05 起兩個 app 都會重探。**
`apps/anilalm/src/asr/useAsrInput.ts` 與 `apps/anila-shell/src/asr/useAsrInput.js`
(首頁 `/` 的聊天室在用的那一份)都改成每 60 秒重探一次:**只在分頁看得見時跑**,
分頁切回來立刻補一次,WebSocket 出錯也立刻補一次;**錄音中那一輪跳過**(不然按鈕
會在使用者講話講到一半消失)。

原本兩邊都只在載入時探一次 → 解碼端**開頁之後**才掛掉,會留下一顆「按了就壞」的
按鈕。遠端解碼端斷斷續續的機率遠高於本機容器,而首頁聊天室是大多數人真正在用的
入口,所以這條在遠端部署下是必要的。實測(瀏覽器,解碼端改回 503):
改動前 → 100 秒後麥克風仍在、探測次數 0;改動後 → 60 秒時按鈕消失、背景分頁期間
探測 0 次、切回前景 0.5 秒內恢復。

代價:解碼端掛掉之後按鈕最多多留 60 秒,那 60 秒內按下去會拿到明確的錯誤訊息
(WS 連不上),不是靜默。**別把間隔調小** —— `/asr/health` 會真的向解碼端送一次
探測請求(openai 協定是 100 ms 靜音的辨識),縮到 10 秒等於拿使用者的分頁去打
算力中心,流量 ×6。

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

`asr-decoder` 掛在 `["asr","asr-local"]`,`asr-gateway` 掛在 `["asr","asr-remote"]`。

**讓「只起 gateway」成立的是 `depends_on: asr-decoder` 上那行 `required: false`,
不是 profile 的分軌。** 相依服務不在啟用的 profile 裡時,少了那一行 Compose 會直接
拒收整份檔案 —— `service "gw" depends on undefined service "dec": invalid compose
project`,連 `up` 都進不去。`asr-local` 只是順手給「只想起本機 decoder」一個名字
(`--profile asr-local`,實測可用),不是遠端部署的前提。
(2026-08-05 用 Compose v2.36.2 最小重現;先前這裡把兩者寫成一組條件,是錯的因果。)

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

## 3c. asr-gateway 開不了機 / 位址被出向檢查擋下來

`ASR_DECODE_URL` 與治理中心指派的位址**兩條路都會過 SSRF guard**
(`validate_outbound_url(..., endpoint_kind="model")`)。沒過就是**啟動時就停**,
不是跑一跑才壞 —— 日誌裡會有:

```
RuntimeError: ASR_DECODE_URL 未通過出向檢查(<reason>): ...
```

`<reason>` 就是分診碼:

| reason | 意思 | 怎麼修 |
|---|---|---|
| `scheme` | 位址是 `http://`,而純 http 沒被放行 | 設 `ANILA_ALLOW_HTTP_ENDPOINT=1` |
| `single_label` | host 是單標籤(多半是 docker 服務名,如 `asr-decoder`) | 把它加進 `ANILA_TRUSTED_HOSTS` |
| `private_ip` | 位址指向 RFC1918 私網 | 優先用 `ANILA_TRUSTED_HOSTS` 點名該主機;真的要開整段才動 `ANILA_ALLOW_PRIVATE_ENDPOINT` |
| `unsafe_ip` | 迴環(`127.0.0.1`)、link-local | **沒有旗標救得了** —— 實測把它同時放進 `ANILA_TRUSTED_HOSTS` 並開兩個旗標,照樣 refused。位址填錯了 |
| `deny_host` | `localhost`、cloud metadata(`169.254.169.254`)等拒絕清單 | 同上,硬擋 |
| `internal_zone` | 命中內部網域清單 | 位址填錯了,或那台真的不該被連 |

(完整清單是 `packages/anila-core/src/anila_core/security/url_guard.py` 的 `REASON_*`。)

### ⚠ 本地語音現在也依賴 `ANILA_ALLOW_HTTP_ENDPOINT`

本地解碼端的位址是 `http://asr-decoder:9000` —— **純 http、而且是單標籤**,兩個條件
都要放行。實測(2026-08-05):

| `ANILA_ALLOW_HTTP_ENDPOINT` | `ANILA_TRUSTED_HOSTS` | 結果 |
|---|---|---|
| `1` | 含 `asr-decoder` | 正常啟動 |
| `0` | 含 `asr-decoder` | **拒絕,reason=`scheme`** |
| 未設 | 含 `asr-decoder` | **拒絕,reason=`scheme`** |
| `1` | 不含 | 拒絕,reason=`single_label` |

**`ANILA_TRUSTED_HOSTS` 救不了 `http://`** —— scheme 檢查排在主機名檢查前面,先撞先死。

`.env.example` 出廠是 `ANILA_ALLOW_HTTP_ENDPOINT=1`,所以新站台不會遇到;會遇到的是
**把它收緊成 `0`** 的站台 —— 收緊之後本地語音會直接開不了機。
(`.env.example` 的註解原本寫「兩個 opt-in 都維持 0(最嚴)」,跟它自己下一行的 `=1`
矛盾,是那個矛盾在鼓勵人去「改正」它;已一併更正,並在那裡註明代價。)
這是 fail-loud、可接受,但要知道是自己關的,不要去查網路。
遠端 openai 端點走 https 時不受影響。

### 部署的 SSRF guard 跟 repo 不一定同一份

asr-gateway 的 `url_guard.py` 是 **build 時複製進 image 的拷貝**,沒有 bind mount,
`up -d` 不會重建 image。改了 `packages/anila-core/.../url_guard.py` 之後只 `up -d`,
gateway 會**繼續用舊規則,而且沒有任何錯誤訊息**。比對指紋:

```bash
docker exec anila-restart-asr-gateway-1 cat /app/.url_guard.sha256
sha256sum packages/anila-core/src/anila_core/security/url_guard.py
```

兩邊不同 → `docker compose -p anila-restart build asr-gateway && ... up -d asr-gateway`。

- 指紋檔是 2026-08-05 才加進 Dockerfile 的。**`cat` 回 `No such file` 就代表這個
  映像比那天更舊** —— 那本身就是答案,直接重建。
- csp 與 router 是同一個形狀(build 時複製、`up -d` 不重建),只是它們還沒有這個
  指紋檔。懷疑它們漂了,只能用 `docker compose build` 重建來排除。

### `/asr/health` 目前是**不認證**的

它會回 `decode_url`(已剝掉 URL 裡的 userinfo,但主機與埠是明文)。本地部署時那只是
`http://asr-decoder:9000`;**接上算力中心之後,那個欄位就是算力中心的位址**。
金鑰不在裡面(`decode_credential_source` 只說來源、不說值)。這是既有狀態、這一批沒有
改變它 —— 記在這裡是為了讓「要不要在 nginx 上把 `/asr/health` 關進內部」變成一個
有人做過決定的問題,而不是沒人注意到的事。

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
那種「能用但難用到沒人想用」比明著壞更糟。

⚠ **但要知道那個 fail-loud 換了樣子。** 搬家前它一定是「`up` 當場失敗、訊息就在螢幕上」;
現在只有**疊了 `asr-gpu.yml`** 才是那樣。**兩個覆蓋檔都沒疊**時 `ASR_DEVICE` 仍預設 `cuda`,
於是:

| | 症狀 | 多久看得出來 | 訊息在哪 |
|---|---|---|---|
| 疊 `asr-gpu.yml` | `up` 直接失敗 | 立刻 | `up` 的輸出 |
| 都沒疊 | asr-decoder 起得來 → 載模型時死 → 重啟迴圈;asr-gateway 卡在 `depends_on: service_healthy` | 最多 20 × 30s ≈ **10 分鐘** | `docker logs anila-restart-asr-decoder-1` |

一樣不會偷偷用 CPU 跑大模型,但慢十分鐘、而且要自己去翻 decoder 的日誌。
沒有 GPU 又要語音,就明確選一邊:`asr-cpu.yml`(小模型)或 `--profile asr-remote`(交給算力中心)。

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

> 若收到 4401,先確認已登入且 cookie 是 `anila_access_token`;其餘見 §7(歷史與契約)。

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

## 7. 權杖契約(已對齊 anila-studio;原「一律 4401」阻斷已關閉)

**現況(2026-07-31 起,`bd63a48f` 與後續契約測試):** asr-gateway 接受 csp
`create_tokens()` 實際簽出的 access token,cookie 名與 studio 相同。麥克風按下去
不應再因「缺 aud / 找錯 cookie」一律 4401。卡登與帳密走同一簽發路徑,行為一致。

**曾存在的兩個缺陷**(都在 `services/asr-gateway/app/auth.py`,不在基礎設施;
已修,留作回歸說明):

1. **Cookie 名稱對不上(已修)。**
   csp 一律簽發 `anila_access_token`
   (`services/csp/app/middleware/cookies.py:33`,與 `COOKIE_SECURE` 無關);
   `anila-studio` 也只讀這個名字(`services/anila-studio/app/auth.py:38`)。
   舊 gateway 在 `COOKIE_SECURE=true` 時找 `__Host-anila_access_token`,false 時找
   `anila_dev_access_token`——兩個名字平台上從來沒人簽發。現在改為只讀
   `anila_access_token`(`services/asr-gateway/app/auth.py` 的 `ACCESS_COOKIE_NAME`)。

2. **Claim / session-assurance 要求對不上(已修)。**
   舊 gateway 要求 `exp/iat/iss/aud/jti` 與 assurance 的
   `jti/sid/amr/acr/auth_time`。csp 的 `create_tokens()` 只簽
   `sub / username / role / tv`,再由 `create_access_token()` 補 `exp` / `type`。
   實測錯誤曾是 `JWTError: missing required key "aud" among claims`。
   **卡登也走同一個 `create_tokens()`**,所以不是「只有帳密才壞」。

**簽章本身一直是好的** —— gateway 從 csp JWKS 依 `kid` 取公鑰驗 RS256;信任錨
未動。修的是「必填一組沒有簽發者會給的 claim」與「讀一個沒人設的 cookie 名」。

**兩條路與抉擇(誠實紀錄):**

| 選項 | 做法 | 為何(不)採 |
|---|---|---|
| (A) | 讓 csp `create_tokens()` 平台級補上 iss/aud/jti/sid/amr/acr/auth_time | 語意較完整,但動全平台權杖格式;發佈前夜不做 |
| (B) **已採** | 把 gateway 要求降到與 anila-studio 一致(`verify_aud=False`,不要求 assurance;仍驗簽章 / kid / 演算法白名單 / exp / `type=access` / `tv` 撤銷) | studio 已對同一 csp 權杖長期可用;範圍小、可驗證 |

採 (B) 的語意:**gateway 不再宣稱在驗證認證強度** —— 因為簽發端從不給那些
claim,舊檢查從未對真實權杖執法過,只是 fail-closed 擋住所有人。真要執法強度,
必須先做 (A),再把 assurance 檢查加回。`tv` 撤銷與 studio 相同:讀 claim `tv`,
經 Redis deny-list `is_revoked(user_id, token_version)` fail-closed。

**回歸測試:** `services/asr-gateway/tests/test_auth_contract.py`(csp 形狀權杖
必過;壞簽章 / 過期必拒;cookie 字面值 `anila_access_token`;撤銷邊界)。

**活體抽查**(可選;不要把 token 貼進 log):

```bash
# 1) 取一個真的 access token(卡登或帳密皆可),存成 token.txt,不要印出來
# 2) 經 nginx 開 WS,應先收到 listening,而非立刻 4401
#    cookie 名必須是 anila_access_token(與瀏覽器 DevTools Application 一致)
```

按下麥克風應依序收到
`{"type":"status","state":"listening",...}` → `{"type":"partial"}`(數次)→
`{"type":"final","text":...}`。

### 已經驗過的部分(不必重驗)

2026-07-31 在本機實測,**認證以下的每一層都是好的**(修復前也成立;阻斷只在
gateway 驗章契約):

- nginx `/asr/` 路由、TLS、WS upgrade:WS 握手成功(HTTP 101),
  舊失敗發生在握手**之後**的應用層 close,不是路由問題。
- gateway → decoder 的網路與 `X-Token` 認證:正確 token 200,錯的/沒帶都 401。
- decoder 真的會辨識:用 `ffmpeg -f lavfi -i flite=text='...'` 合成一段已知英文語句
  (repo 內沒有音訊 fixture,而這台機器沒有 espeak/pico2wave/flite CLI,
  但 ffmpeg 內建 `flite` filter 可以合成),餵 `POST /transcribe` 得到
  逐字一致的辨識結果。

契約對齊後,語音應可端到端使用;若仍 4401,先查 cookie 名與是否已登入,再查
撤銷清單 / JWKS(4503),不要先懷疑「又缺 aud」。
