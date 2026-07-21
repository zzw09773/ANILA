# ANILA 串流語音輸入（ASR）實作規劃書

> 狀態:規劃中(2026-07-17 起草;同日依資深審查修訂 v2 —— 修掉 §0 與 §10 的自相矛盾、
> 補 nginx 兩顆地雷、把 async 編排/WS 認證/前端預覽寫成明確規格,分支 `feat/asr-voice-input`)。
> 本文件供執行開發的 agent(Opus)照章實作;動手前先讀 `AGENTS.md` 全文與 `CLAUDE.md`。
> 目標:聊天輸入框旁新增麥克風按鈕,按下後即時串流辨識(邊講邊出字),
> 辨識結果填入輸入框,使用者可修改後送出。文字輸入照舊,ASR 是附加輸入途徑。

---

## 0. 一頁總覽

```text
瀏覽器(anilalm / anila-shell 聊天輸入框)
  │  麥克風按鈕 → getUserMedia → AudioWorklet → Int16 PCM @16 kHz
  │  WSS /asr/stream(同源 WebSocket,握手自動帶 access-token cookie,名稱雙軌見 §2.2)
  ▼
nginx(既有 TLS 門面;新增 location /asr/ 的 WS upgrade 轉發)
  ⚠ 必須同時放寬 Permissions-Policy 的 microphone(§2.4)——
    不放寬的話 getUserMedia 一律 NotAllowedError,按鈕永遠拿不到麥克風
  ▼
asr-gateway(★新服務,platform stack,無 GPU、無 DB)
  - FastAPI WebSocket;JWT 驗證抄 anila-studio 的 auth.py 模式(cookie/Bearer + JWKS + revocation)
  - webrtcvad 切句狀態機:移植參考專案的 StreamingTranscriber(見 §2)
  - partial(預覽)/ final(定稿)雙軌解碼事件
  - 繁體靠 initial_prompt;OpenCC **預設關閉**(⚠ 實測證據見 §10 ——
    本文件初版寫的「s2twp final 必做」已被實測推翻,不要照直覺加回來)
  │  HTTP POST /transcribe(X-Token 共享密鑰)
  ▼
asr-decoder(★新服務,GPU,獨立容器)
  - faster-whisper;模型尺寸/精度全走環境變數,可隨部署替換
  - 本機 dev 掛 anila-models stack;內網可獨立跑在任何 GPU 主機(MLSteam 模式)
```

兩個新服務、零 DB 變更、零 alembic migration。音訊**只存在記憶體,不落地、不記錄**(§6)。

### 為什麼拆成 gateway + decoder 兩個容器

1. **對齊平台既有邊界**:`anila-platform` stack 沒有任何 GPU 服務,GPU 全在獨立的
   `anila-models` stack(`infra/models/docker-compose.yml`,external network
   `anila-models-net`,不開 host port)。ASR 照做。
2. **內網部署現實**:內網 GPU 可能在 MLSteam(aiops)或其他主機,不一定跟平台主機
   (.15)同機。decoder 做成「一個 HTTP 端點 + shared token」就能跟 anila-agent 一樣
   丟到任何地方,gateway 只要改 `ASR_DECODE_URL`。
3. **參考專案已驗證此拆法**:`streaming_asr` 的 Architecture A 就是「web/VAD 端 +
   遠端 GPU 解碼」,decode 契約現成(§2.1)。

---

## 1. 參考來源(先讀再寫)

| 路徑 | 拿什麼 |
|---|---|
| `~/Desktop/tenya/語料庫/ASR_intranet/code_for_mlsteam/streaming_asr/transcriber.py` | **幾乎整檔移植**到 gateway:webrtcvad 狀態機、preroll、partial/final 雙軌、幻覺過濾(`HALLUCINATION_*_RE`)、degenerate 壓縮比檢查、RMS 門檻。UI-free、decode_fn 可插拔,設計就是給這種場景用的。 |
| 同目錄 `app.py` | decoder 的 HTTP 契約(`make_remote_decode`):POST `/transcribe?kind=&beam=&prompt=&language=zh`,body=Int16 PCM bytes,回 `{"text","no_speech_prob","avg_logprob","decode_seconds"}`。partial timeout 4s 快棄、final 30s 苦等的策略照抄。faster-whisper 的 partial(greedy、關門檻)vs final(beam、開門檻)參數組也照抄。 |
| 同目錄 `templates/index.html`(629 行起) | 瀏覽器錄音:AudioWorklet `PCMForwarder` + 重採樣到 16kHz + Int16 轉換 + ScriptProcessor fallback。改寫成 React hook。 |
| `~/Desktop/tenya/語料庫/asr_ui/RealtimeStt.Backend/README.md` | UX 語意參考:partial 是「整段重取代」不是 append;靜音 commit 一行。技術棧(C#/SignalR)不沿用。 |
| `services/anila-studio/app/auth.py`、`services/jwks_client.py`、`services/revocation_cache.py` | 跨服務 JWT 驗證的整套現成模式:access cookie(名稱雙軌,見 §2.2)或 Bearer、JWKS 拉自 csp、Redis revocation channel、cold-start 全量同步。gateway 照抄結構。 |
| `infra/compose/platform.yml` 的 `anila-studio` 區塊(約 545–617 行) | 新服務 compose 定義樣板:fail-loud env(`:?must be set`)、locked image ID、healthcheck、depends_on、restart policy。 |

---

## 2. 服務規格

### 2.1 asr-decoder(`services/asr-decoder`)

單一職責:收 PCM、回文字。無狀態、無 session 概念。

- **技術**:Python 3.12 + FastAPI + faster-whisper(CTranslate2)。
- **API**:
  - `POST /transcribe?kind=partial|final&beam=<n>&prompt=<s>&language=zh`
    - body:`application/octet-stream`,Int16 mono PCM @16 kHz
    - header:`X-Token: <ASR_DECODER_TOKEN>`(不符回 401)
    - 回:`{"text","no_speech_prob","avg_logprob","decode_seconds"}`
    - 解碼參數依 kind 分流,照 `app.py` `make_local_decode` 的兩組 options。
  - `GET /health`:回模型名/裝置/是否 ready(供 compose healthcheck 與 admin 檢視)。
- **併發**:單 model instance + `threading.Lock` 序列化(參考碼同款)。
  ⚠ 「十人級併發」指的是**同時在線 session**,不是**同時講話的人**——
  真實承載與負載旋鈕見 §8.1 第 2 條(待拍板)。
- **環境變數**(全部可換,模型選型刻意延後):
  - `ASR_MODEL_SIZE`(`medium` | `large-v3`,dev 預設 `medium`,正式建議 `large-v3`)
  - `ASR_DEVICE`(`cuda`/`cpu`)、`ASR_COMPUTE_TYPE`(**V100 用 `float16`,不要 int8**——Volta 無 int8 tensor core,int8 反而更慢;官方 V100S benchmark:fp16 54s vs int8 59s)
  - `ASR_DECODER_TOKEN`(必填 fail-loud)
  - `ASR_MODEL_DIR`(權重目錄,air-gap 用 volume 掛入;見 §7)
- **Dockerfile**:`nvidia/cuda:12.x-cudnn9-runtime` 基底(CTranslate2 4.x 需 CUDA 12 + cuDNN 9)。權重**不烘進 image**,volume 掛載,image 保持可簽章、權重可獨立替換。
- **啟動即暖機**:load 後跑一次 0.5s 靜音解碼(參考碼同款),health 才轉 ready。
- ✅ **`_load_or_die` 已修(2026-07-18,M7)**:model load 失敗時 `os._exit(1)`
  讓整個 process 以非零碼退出,`restart: unless-stopped` 才會真的重試(暫時性
  CUDA 故障靠這個救回來)。早期版本只 log、process 續活,以為「health 一直 503
  → compose 會判 unhealthy 重啟」——那是錯的:docker compose 沒有 autoheal,
  unhealthy 容器不會被重啟,restart policy 只管 process 死亡,結果是永久 503 的
  殭屍。**用 `os._exit` 而非 raise/`sys.exit`**:load 跑在背景 daemon thread,
  例外傳播出不了那條 thread,只有 `os._exit` 能從整個 process 出去。已用子行程
  實測 exit code = 1。
- **部署位置(已拍板:兩個版本都交付,同一份程式碼、只差設定)**:
  - **內部版(平台主機 GPU)**:decoder 進 `infra/models/docker-compose.yml`,
    掛 `anila-models-net`;gateway 的 `ASR_DECODE_URL=http://asr-decoder:9000` 走內部
    network,不開 host port、不需 http 放行旗標。本機 dev 也用這版。
    **內部版是內網優先選項**(語音不出主機,見 §6)。
    ⚠ 釘哪張卡是**待決問題**(本機四張卡都有主、`.15` 有無 GPU 未查證),
    見 §8.1 第 1 條;M4 動 compose 前必須先拍板。
  - **外部版(獨立 GPU 主機,如 MLSteam/aiops)**:decoder 單容器 `docker run`(附
    獨立的 `docker-compose.standalone.yml` + README 一頁部署步驟),對外 http
    NodePort,同 anila-agent 模式;gateway 設
    `ASR_DECODE_URL=http://<gpu-host>:<port>` + `ASR_ALLOW_HTTP_DECODER=1`,
    `X-Token` 為唯一防線,**token 必須夠長且不進 repo**。
    ⚠ 純 http = 語音明文過內網,是**書面風險接受項**,見 §6。
  - 兩版的 `.env.example` 範例區塊與部署文件(§4 的 docs)都要寫,operator 二選一。

### 2.2 asr-gateway(`services/asr-gateway`)

- **技術**:Python 3.12 + FastAPI(原生 WebSocket)+ webrtcvad + numpy。CPU-only、無 DB、無 Redis 持久狀態(revocation cache 訂閱除外)。OpenCC 只在 `ASR_OPENCC_MODE` 非 `off` 時才 import(預設不啟用,見 §10)。
- **端點**(⚠ **路由一律自帶 `/asr` 前綴**:nginx 的 `location /asr/` 不 strip
  prefix(§2.4),所以是 `/asr/stream`、`/asr/health`。compose healthcheck 與
  M4 驗證都打 `/asr/health` —— 打 `/health` 得到 404 是路徑錯,不是服務掛了,
  別回報假陰性):
  - `WS /asr/stream`:主通道。
  - `GET /asr/health`。
- **WS 協定**(前後端唯一契約,實作時先定 schema 再寫兩端):
  - client→server:binary frame = Int16 PCM chunk;text frame = JSON 控制訊息
    `{"type":"flush"}`(使用者按停,強制收尾當前語段)。
  - server→client:text frame JSON:
    - `{"type":"status","state":"idle|listening|recording","msg":...}`
    - `{"type":"partial","id":<utt>,"text":...}`(同 id 整段取代)
    - `{"type":"final","id":<utt>,"text":...,"latency_seconds":...}`
    - `{"type":"discard","id":<utt>}`(太短/太小聲/幻覺,前端把該 id 的 partial 清掉)
    - `{"type":"error","msg":...}`
  - **協定不變式(雙方都要守)**:同一 utt id 的 `final` 或 `discard` 一旦送出,
    **之後不得再送該 id 的 `partial`**;前端收到違反此規則的 partial 一律忽略
    (雙保險)。沒有這條,遲到的 stale partial 會在定稿後讓預覽文字復活且
    永遠清不掉(下一個 final 是別的 id,清不到它)。
- **認證**(結構照抄 anila-studio;以下是長連線特有的差異,實作照做、不要另發明):
  - 握手時從 cookie 或 `Authorization: Bearer` 取 JWT → JWKS 驗章 → 查 revocation。
    `CSP_BASE_URL`、`JWT_ISSUER/AUDIENCE/LEEWAY`、`REDIS_URL` + revocation channel
    那組 env 全部對齊 studio。
  - **cookie 名要跟 studio 一樣雙軌**:`COOKIE_SECURE=true` 時是
    `__Host-anila_access_token`,`false`(本機 dev)時是 `anila_dev_access_token`
    (照抄 studio `auth.py` 的 `_access_cookie_name`)。瀏覽器的 WebSocket API
    **不能帶 Authorization header** —— 對瀏覽器而言 cookie 是唯一路徑,Bearer
    只有測試/服務間在用。只做 `__Host-` 的話,M5 本機 dev 必 401。
  - **close code 要送得到前端,就必須先 accept**:Starlette 對「未 `accept()`
    就 `close()`」的 WS 回 HTTP 403 拒絕握手,前端 `onclose` 只看得到 1006,
    **永遠讀不到自訂 code**。規格:一律 `accept()` 後立刻 `close(code)`。
    code 表:**4401** 驗證失敗/token 無效、**4408** session 逾時、**4409**
    併發超限(被新連線踢掉的舊連線收的也是 4409)。M3 測試要驗「client 端
    真的收得到這些 code」。
  - **revocation 長連線重查(已定案)**:握手驗過之後,session loop **每 30 秒
    對 in-memory revocation cache 重查一次**(cache 本來就靠 Redis channel 即時
    更新,重查只是本地查表、零 I/O);查到 revoked → `close(4401)`。不採
    「另訂閱 channel 推播斷線」—— 多一條訂閱路徑只換到 <30s 的時效差,不值。
  - **token 過期(明確的接受決策,不是沒想到)**:握手驗一次,session 存活
    期間不重驗 exp。最壞情況 = 握手時 token 剩 1 秒過期,仍可講滿
    `ASR_MAX_SESSION_SECONDS`(預設 300s)。access token 60 分鐘、session 上限
    5 分鐘,這個窗口可接受;**日後若把 session 上限調大,必須回頭重評這條**。
- **切句/解碼**:移植 `transcriber.py` 的 `StreamingTranscriber`(已完成,見 §5
  M2)—— 純同步狀態機,解碼與 WS 推送在 `session.py` 的 async 層。decode_fn 用
  `make_remote_decode`(httpx 版,替換 urllib;partial timeout 4s、final 30s 照舊)。
- **async 編排層(session.py)必守規則**(審查 2026-07-17 定;照做,不要重新發明):
  1. **final 優先、partial 可丟**:同一批 events 裡 `PartialReady` 後緊跟同 utt
     的 `FinalReady` 時,partial 直接跳過;佇列裡有 final 待解時不送 partial。
  2. **每 session 同時最多一個 in-flight partial**:上一個 partial 解碼未回來前,
     新的 `PartialReady` 塌縮成「最新一次」;真正要解的那一刻才向
     `partial_snapshot()` 拿音訊,拿到空(語段已收尾)就放棄該次 partial。
  3. **stale partial 必須丟棄**:partial 解碼是 await 的遠端呼叫,回來時該 utt
     可能已 final/discard。session 要記錄每個 utt 的終態,凡「已終態 utt 的
     partial 結果」一律不推送(= 上方協定不變式的 server 端實作)。
  4. **棄單不會被取消(要知道,但先不處理)**:httpx 4s timeout 放棄的 partial,
     decoder 仍在 lock 裡把它解完 —— 取消不傳播。規則 1、2 就是為了讓棄單數量
     有上界;若實測仍出現排隊雪球,再議 decoder 端 stale-drop(gateway 帶
     deadline header、decoder 進 lock 前檢查過期即棄),**不要先做**。
  5. **清理一律 try/finally**:per-user 併發名額、in-flight httpx 請求、
     segmenter 狀態,任何 exception 路徑都必須釋放 —— 否則一次未捕捉例外就把
     該 user 永久鎖死在 4409。
  6. **session 逾時先 flush 再關**:`ASR_MAX_SESSION_SECONDS` 到點時先
     `flush()`、等 final 回來(給短暫上限,例如 5s)再 `close(4408)`;
     不 flush 直接斷 = 默默丟掉使用者最後一句。
  7. **decoder 掛掉不 crash**:final 解碼失敗 → `{"type":"error"}` + 該 utt
     `discard`;partial 失敗靜默丟(參考實作同款)。失敗只影響該 session,
     不得讓 gateway process 死掉。
- **繁中後處理:預設不做 OpenCC**(⚠ 2026-07-17 實測推翻本文件初版的假設,見 §10)。
  只靠 `initial_prompt`(預設「以下是繁體中文。」)—— 這也是內網參考實作的選擇
  (`transcriber.py:302` `text = raw  # traditional comes from the prompt`)。
  幻覺過濾清單裡保留 prompt 回音項(「以下是繁體中文」)。
  留一個 `ASR_OPENCC_MODE=off|s2t|s2tw`(**預設 `off`**)當保險:改用 large-v3 後若
  實測出現簡體再開,且要重跑 §10 的誤傷量測再決定模式。**不要用 `s2twp`**。
- **資源防護**:
  - 每 user 同時最多 1 條 ASR session(依 JWT sub 計)。**踢舊留新**:第二條
    連上來時關掉舊連線(對舊連線 `close(4409)`)、接受新的 —— **不是拒絕
    新的**。理由:筆電闔蓋/斷網留下的死 TCP,gateway 察覺不到;「拒新」會把
    使用者鎖在門外直到舊 session 逾時。另加 **WS ping/pong**(例如每 20s)
    偵測死連線提早回收。
  - 單 session 最長 `ASR_MAX_SESSION_SECONDS`(預設 300)—— 逾時處理見上方
    編排規則第 6 條(先 flush、再 4408)。
  - 進站 PCM 速率上限 ~32 KB/s(16kHz×2bytes)+ 緩衝上限,超量斷線——防灌流量。
  - **單一 process 假設**:per-user 計數放在記憶體,uvicorn 必須 `--workers 1`
    (gateway 無狀態、CPU 輕,單 worker 夠用);要 scale 再議 Redis 計數,
    不要默默開多 worker 讓限制失效。
- **環境變數**:`ASR_DECODE_URL`(必填)、`ASR_DECODER_TOKEN`(必填)、
  `ASR_INITIAL_PROMPT`、`ASR_BEAM_SIZE`(預設 5)、`ASR_PARTIALS_ENABLED`(預設 1)、
  `ASR_MAX_SESSION_SECONDS` + studio 同款 JWT/Redis 那組
  + **`CSP_SERVICE_TOKEN`(審查補,初版漏列)**:revocation cache 冷啟動要拿它
  以 `X-CSP-Service-Token` 打 csp 的 `/api/auth/revocations` 做全量同步
  (見 platform.yml anila-studio 區塊的同名 env)。漏了它 cache 永遠不 ready,
  fail-closed 之下**所有 WS 一律被拒**,而且症狀長得像 auth 壞掉。
- ⚠ **SSRF 姿態**:`ASR_DECODE_URL` 是 operator 設的 env,不是使用者輸入,不經
  SSRF guard;但 gateway 出向**只允許**這一個 URL,程式內不得有任何由 client 決定
  目的地的請求。內網 decoder 若走 http(MLSteam NodePort 模式),沿用
  `ANILA_ALLOW_HTTP_*` 的旗標慣例新增 `ASR_ALLOW_HTTP_DECODER=0` 預設關。

### 2.3 platform.yml 接線

- 新增 `asr-gateway` service:樣板抄 anila-studio(fail-loud env、healthcheck、
  `restart: unless-stopped`);`expose: "8200"`;`profiles: ["asr"]` →
  **預設不啟動,要用的部署 `COMPOSE_PROFILES=asr`**。理由:七條分支共用
  platform.yml,ASR 未必每個部署都開,profile 讓不開的分支零影響。
  ⚠ nginx 端必須配合**變數式 proxy_pass**,否則 profile 沒開時 nginx 起不來,
  見 §2.4。
- **image 走 locked content ID 慣例**(審查補):
  `image: ${ANILA_IMAGE_ASR_GATEWAY:?ANILA_IMAGE_ASR_GATEWAY must be a locked image content ID}`,
  對齊 platform.yml 既有服務與 Gate 2 signed profile 的要求;不要寫死 tag。
- **networks**(審查補):內部版 gateway 要打 `http://asr-decoder:9000`
  (decoder 在 `anila-models-net`),所以 gateway 必須掛
  `networks: [default, anila-models-net]` —— 寫法照抄 csp
  (platform.yml:279–284)。只掛 default 會 DNS 解不到 decoder。
- **故障矩陣(部署文件必寫)**:gateway 的 revocation 是 fail-closed(對齊
  studio)—— **Redis 或 csp 倒 → ASR 全斷**(WS 一律被拒)。這是刻意的安全
  姿態;operator 文件的「ASR 不能用」排查表必須把這兩個上游列在最前面。
- decoder 進 `infra/models/docker-compose.yml`(本機 dev 用),內網另行部署;
  `device_ids` 待 §8.1 拍板後才寫。

### 2.4 nginx

- `map $http_upgrade $connection_upgrade` 已存在(anila.conf:51),直接用。
- ⚠ **必須用變數式 proxy_pass,不能寫死 upstream(審查抓到的地雷)**:
  asr-gateway 是 profile-gated、預設不啟動。寫死
  `proxy_pass http://asr-gateway:8200;` 的話,nginx 在**載入設定時**就解析該
  hostname,容器不存在 → `nginx: [emerg] host not found in upstream` →
  **nginx 拒絕啟動,整個平台入口跟著掛**;七分支同步後,每個沒開 asr profile
  的部署都炸。anila.conf 檔頭(5–14 行)的 resolver 註解與 `codeserver`
  (同為 profile-gated)的 `set $codeserver_addr ...` 寫法,存在的理由正是
  這個 —— 照抄那個 pattern;**不要**照抄 `/router/`(它的 upstream 是常駐服務)。
- 新增(兩個 conf:`anila.conf`、`anila-gate2-pilot.conf`;各自的 443 與 4443
  platform server block 都要):

```nginx
location /asr/ {
    limit_conn conn_limit 20;

    set $asr_gateway "asr-gateway:8200";   # 變數式 → 每請求經 resolver 解析,
    proxy_pass http://$asr_gateway;        # asr profile 沒開時 nginx 照樣起得來
    proxy_http_version 1.1;
    proxy_set_header Upgrade           $http_upgrade;
    proxy_set_header Connection        $connection_upgrade;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3600s;   # 長連線;session 上限由 gateway 自己管(§2.2)
    proxy_send_timeout 3600s;
}
```

  (nginx 不 strip `/asr` 前綴 → gateway 路由自帶前綴,見 §2.2。cookie 預設
  就會轉發,不必另設 `proxy_set_header Cookie`。)
- ⚠ **Permissions-Policy 必須放寬 microphone,否則整個功能做不出來(審查抓到,
  初版完全沒想到)**:平台 server block 目前對所有回應加
  `Permissions-Policy: ... microphone=(), ...`(anila.conf:137 與 :531;
  gate2-pilot conf 同位置;`/uploads/`、`/uploads/flux/` location 另各有一份)。
  這是 document 層級的硬封鎖 —— 即使 HTTPS、即使使用者按「允許」,
  `getUserMedia` 一律拋 `NotAllowedError`,**症狀跟「使用者拒絕權限」完全
  分不出來**;不先改這裡,M5 實測會浪費大把時間查錯方向。改法:
  - **只**把「服務聊天 UI 的 platform server block」(443 與 4443,兩份 conf)
    改成 `microphone=(self)`;
  - n8n / gitlab / codeserver 隔離 origin 與 `/uploads/`、`/uploads/flux/`
    location 的 header **維持 `microphone=()` 不動**(那些 origin/位置沒有
    任何理由碰麥克風);
  - **連同該行上方的註解「嚴格 Permissions-Policy — 平台不需要這些 sensor /
    媒體 API」一起改寫**(註明 microphone 因 ASR 放寬為 self)—— 不改註解,
    下一個讀到它的人會把 header「修回去」,ASR 無聲暴斃。
  - 這是安全姿態的放寬,屬通用 infra 改動,七分支同步時要一起帶(§9)。
- 若該部署有 card-session 的 `auth_request` 慣例,`/asr/` 比照掛上(gateway 內
  仍自驗 JWT,雙層不衝突)。

### 2.5 前端

**已拍板:`anilalm` 與 `anila-shell` 兩個都做**。錄音/WS 邏輯寫成一份可共用的
hook(框架無關的 core + 薄薄的 React 綁定),兩個 app 各自接進自己的輸入列;
**先在 `anilalm`(WSChat.tsx)接通並驗證全鏈路,再把同一份 hook 接進
`anila-shell`(chat.jsx)**——第二個 app 只剩 UI 接線,不重寫邏輯。
注意兩個 app 是分開的 build(anilalm 是 TS、anila-shell 是 jsx),共用碼放
`packages/` 或以複製檔案方式各自 vendor,由實作者依 repo 既有共用慣例決定
(先查 `packages/` 有沒有前端共用包的前例)。

- **錄音 hook**(`useAsrStream` 之類):改寫參考 `index.html` 的
  AudioWorklet PCMForwarder + 重採樣 + Int16 打包;WS 連 `/asr/stream`;
  處理 partial(同 id 取代)/final(append)/discard(清 partial)事件;
  **遵守 §2.2 協定不變式**:final/discard 之後同 id 的 partial 一律忽略;
  WS 斷線時清掉所有 partial 預覽。
- **麥克風按鈕**:輸入框旁 icon。狀態機:
  `idle → requesting(要權限) → connected/listening(聆聽) → recording(講話中,VAD 觸發) → idle`。
  錄音中再按一次 = 送 `flush` + 停止。錯誤(權限拒絕、WS 斷、close
  4401/4408/4409)給非阻斷 toast,不影響文字輸入;4401 提示重新登入、4408
  提示逾時、4409 提示「已被其他分頁/裝置接手」。
- **文字落點(審查後降級為預設方案;初版的「輸入框內灰字 ghost」不做)**:
  final 文字 **append 進輸入框**(既有草稿之後,自動加空格/換行判斷)。
  partial **不放進 textarea** —— 兩邊輸入框都是原生 `<textarea>`
  (WSChat.tsx:479、chat.jsx:1311),textarea 無法混排兩色文字;overlay mirror
  div 要同步捲動/換行/字型是經典坑,contenteditable 等於重寫輸入框(還會動到
  chat.jsx 的 mention 選單與貼上處理)。**預設做法:partial 顯示在輸入框正下方
  一行小字預覽(帶「辨識中…」樣式),final 才進 value。** 日後要做框內 ghost
  另開 spike,不在本功能範圍(§3)。
- **IME(注音)守則(必守)**:使用者組字中(`compositionstart` 後、
  `compositionend` 前)**不得 append final 進 value** —— 會打斷組字、游標亂跳。
  final 抵達時若正在組字,緩衝到 `compositionend` 再 append。
- **`disabled={busy}` 的行為**:WSChat 在 LLM 回覆中會 disable textarea。規格:
  錄音中收到的 final 照樣 append 進 state(setState 不受 disabled 影響),但
  麥克風按鈕在 busy 時不可啟動**新**錄音(避免使用者對著鎖住的輸入框講話)。
- **功能旗標(審查後改為 runtime 判斷,不用 build-time)**:初版規劃的
  `VITE_ASR_ENABLED` 是 build-time 旗標 → ASR 開/關會變成**兩顆內容不同的前端
  image**,與 platform.yml 的 locked image content ID / Gate 2 簽章 profile 走向
  直接打架(每個姿態組合都得多簽一顆 image)。改為 runtime:前端啟動時 probe
  `/asr/health`(asr profile 沒開 → nginx 回 502/404 → 藏按鈕),或併進既有
  capabilities 端點。成本相當,image 不分裂、旗標回到部署層。
- **Secure context**:`getUserMedia` 只在 https/localhost 可用。平台門面本來就是
  nginx TLS,正式環境沒問題;**用 http 直連測試時麥克風必然拿不到**,寫進除錯文件。
  另注意:secure context 只是必要條件之一 —— §2.4 的 Permissions-Policy 沒放寬前,
  麥克風照樣拿不到,**兩個條件缺一不可**(除錯文件兩條都要列)。

---

## 3. 不做什麼(scope 邊界)

- ❌ 不動 csp、不加 DB table、不加 alembic migration(gateway 無狀態)。
- ❌ 不做熱詞/glossary/數字正規化/LLM 校正(參考專案有,先不搬;留擴充點)。
- ❌ 不做語音指令、不自動送出訊息、不做 TTS。
- ❌ 不做輸入框內 ghost 文字(降級為框下一行預覽,理由見 §2.5)。
- ❌ 不動七條分支的旗標姿態;downstream port 是功能落地後的獨立步驟(§9)。
- ❌ 不在此功能裡選定模型尺寸(env 可換,部署期依 GPU 餘裕定案;V100→fp16)。

## 4. 檔案異動清單(預估)

| 動作 | 路徑 |
|---|---|
| 新增 | `services/asr-decoder/`(app.py、Dockerfile、requirements、tests、README、`docker-compose.standalone.yml`(外部版單機部署)) |
| 新增 | `services/asr-gateway/`(app/main.py、app/auth.py(抄 studio)、app/transcriber.py(移植)、app/decode_client.py、app/session.py(規格見 §2.2)、Dockerfile、requirements、tests、README) |
| 新增 | 前端:錄音 hook(共用)+ 麥克風按鈕元件 + `anilalm` WSChat.tsx 與 `anila-shell` chat.jsx 兩邊輸入列接線 |
| 修改 | `infra/compose/platform.yml`(asr-gateway:profile 隔離、locked image ID、雙 network) |
| 修改 | `infra/models/docker-compose.yml`(asr-decoder,本機 dev;device_ids 待 §8.1 拍板) |
| 修改 | `infra/nginx/anila.conf`、`anila-gate2-pilot.conf`(`/asr/` location(變數式 proxy_pass)+ **Permissions-Policy microphone 放寬與註解改寫**,見 §2.4) |
| 修改 | `.env.example`(新 ASR 變數區塊,含註解;註明 gateway 也吃 `CSP_SERVICE_TOKEN`) |
| 新增 | `docs/`(部署/除錯說明:air-gap 權重與 image 打包(§7)、V100 fp16、http decoder 旗標與 §6 風險簽核、§2.3 故障矩陣、麥克風拿不到的兩種原因(§2.5)) |

## 5. 里程碑(每個都可獨立驗收)

1. ~~**M1 decoder**~~ ✅ **已完成(2026-07-17)**。`services/asr-decoder/`;21 個契約測試通過
   (假件注入,不需 GPU/權重/網路);另以 tiny/CPU 實跑真實 faster-whisper 1.2.1 驗過:
   模型載入+暖機、`503→200` readiness 轉換、partial/final 兩組解碼選項均被接受、
   401/400/413 全部如實回應、**log 只出 metadata 不含辨識文字**(實測確認)。
   實測副產物:對純正弦波 whisper 會吐 `《寧域》`、`詞曲 曲曲 曲曲…` 這類幻覺與重複
   迴圈,且 `no_speech_prob≈0.47` 低於 0.5 門檻擋不掉 → **M2 的幻覺/degenerate 過濾
   是必要的,不是保險**。(⚠ 另有一筆審查後的已知待修:`_load_or_die` 的自癒
   假設是錯的,見 §2.1,列入 M7。)
2. **M2 gateway 核心**(進行中):
   - ✅ **切句狀態機已移植**(2026-07-17):`app/transcriber.py` = `VadSegmenter`,
     34 個測試通過。**與原版的差異只有一處且刻意**:原版把解碼扛在自己身上
     (每 session 一條 thread + `Condition`,因為它跑 Flask-SocketIO 的 threading
     模型);本版只留純狀態機(餵 bytes → 吐 event),解碼與 WS 推送交給 async
     層。常數與判斷邏輯逐行照抄。VAD 做成可注入(`vad=` 參數)才測得動 ——
     實測 webrtcvad 的判定與音量天生相關,用真 VAD 無法單獨控制變因。
   - ⬜ 待做:`decode_client.py`(httpx 版 remote decode)+ `session.py`
     (async 編排 —— **規格已定,照 §2.2「session.py 必守規則」七條實作**;
     測試要涵蓋 stale partial 丟棄、in-flight partial 塌縮、try/finally 清理、
     逾時先 flush)。
   - **幻覺過濾要用真實語音驗**,不要對著合成正弦波調參數(§11)。
     `voice_dataset` 的 1096 個 wav 是現成的真實素材,`streaming_asr/test_client.py`
     是現成的灌音腳本。

   移植時查到的兩個既有性質(**照移植,不改**,但要知道):
   - **長度閘門(`MIN_UTTERANCE_SEC`)只有 flush 路徑到得了**。走 VAD 收尾的語段
     必然含 300ms preroll + 510ms 收尾靜音 = 810ms > 300ms,長度檢查必然放行。
     也就是說「使用者按停」是這道閘門唯一的觸發途徑。
   - **preroll 是含觸發幀在內的滑動窗**:進入語段時 speech 長度 = `PREROLL_FRAMES`
     (300ms),不是 preroll + 觸發幀。
3. **M3 gateway WS + auth**:WS 協定 + JWT 驗證 + 併發/時長限制,全依 §2.2 規格:
   **先 `accept()` 再 `close(code)`**(4401/4408/4409 表)、cookie 名雙軌、
   revocation 每 30s 重查、踢舊留新 + ping/pong、`CSP_SERVICE_TOKEN` 冷啟動
   同步。pytest 用 fastapi TestClient 的 ws 介面,測試要驗「client 端真的
   收得到自訂 close code」。
4. **M4 接線**:compose(locked image ID、雙 network;decoder 的 device_ids 需
   先過 §8.1 拍板)+ nginx(變數式 proxy_pass + Permissions-Policy 放寬)+
   `.env.example`;`COMPOSE_PROFILES=asr up -d` 全鏈路起來,**同時驗「不開
   profile 時 nginx 照常啟動」**;`docker exec` 內部打 `/asr/health`(csp 容器
   沒 curl 的教訓:用 python httpx;路徑帶 `/asr` 前綴,打 `/health` 是 404
   不是服務掛了)。
5. ~~**M5 前端(anilalm)**~~ ✅ **已完成(2026-07-18)**。`apps/anilalm/src/asr/`
   (`asrStream.js` 共用核心 + `asrStream.d.ts` 型別 + `useAsrInput.ts` React 綁定)
   接進 `WSChat.tsx`(麥克風按鈕在送出鍵旁、框下小字預覽、IME 事件、錯誤列)。
   `npm run build`(`tsc -b && vite build`)通過。⬜ **瀏覽器實測仍待 user 做**
   (需真人講話 + secure context,自動化測不到 —— M5 起就講明);拿不到麥克風時
   **先查 §2.4 的 Permissions-Policy 是否已放寬**(症狀與權限拒絕無法區分)。
6. ~~**M6 前端(anila-shell)**~~ ✅ **已完成(2026-07-18)**。共用核心 vendor 成
   `apps/anila-shell/src/asr/asrStream.js`(與 anilalm 版位元組相同,md5 對齊)
   + `useAsrInput.js`(jsx 版,因 anila-shell 無 TS);接進 `chat.jsx` 的
   `Composer`(最小侵入:只加 IME 事件、麥克風鈕、預覽列,不碰既有 mention/貼上/
   autosize)。加 `IconMic`。`npm run build` 通過。**vitest 26 個 ASR 測試通過**
   (含 7 個真的驅動 `createAsrSession` 的協定不變式測試:stale partial 丟棄、
   discard 後 partial 無效、跨 utt 獨立、空 final 不 append、壞 JSON 不 crash);
   anila-shell 全 suite 256 passed,沒弄壞既有測試。

   M5/M6 實作時對規格的兩處補充:
   - **共用碼用 vendor 副本,不建前端共用套件**:`packages/` 底下四個全是 Python,
     repo 沒有任何 npm workspace / 前端共用包前例(而且 anilalm 有 TS、anila-shell
     沒有)。要共用得先引入前端 monorepo 工具鏈 —— 那是基礎建設改動,不該夾在
     語音輸入裡。`asrStream.js` 兩份位元組相同副本,檔頭有 VENDORED 警告。這是
     第二筆 vendor 債(第一筆是認證模組 §12),但這次是 ~350 行 UI 邏輯、非安全
     關鍵碼,代價低。
   - **功能旗標用 runtime probe,不用 build-time**:`probeAsrAvailable()` 打
     `/asr/health` —— gateway 是 profile-gated,沒開時 nginx 打不到 → probe 失敗
     → 按鈕自然不渲染。連旗標都不需要,也不會分裂 locked image。(規劃書 §2.5
     原本就傾向 runtime;這裡確認了「連 capabilities 端點都不必碰,probe 就夠」。)
7. ~~**M7 收尾**~~ ✅ **已完成(2026-07-18)**:
   - ✅ **修 `_load_or_die`**:`os._exit(1)`,子行程實測 exit code = 1(見 §2.1)。
   - ✅ **部署文件兩版**:`docs/runbooks/asr-voice-input.md`(內部版/外部版分節、
     §3.0 外部版書面風險簽核、§5 故障矩陣把 Redis/csp fail-closed 列最前)
     + `services/asr-gateway/README.md` + 既有的 `services/asr-decoder/README.md`。
   - ✅ **`AGENTS.md` §10 checklist 自查**(見交付回報):測試齊、無密鑰進 git、
     token fail-loud 無 dev fallback、SSRF 出向只三個固定目的地、compose/nginx
     `-t` 通過。
   - ⬜ **唯一剩餘:瀏覽器實測**(按麥克風→講話→出字)需真人 + secure context,
     自動化測不到 —— M5 起就講明,交由 user 執行;runbook §4 有步驟。

每個 milestone 一個以上獨立 commit;gateway/decoder/前端/infra 改動**分開 commit**(混 concern 要拆)。commit 不帶分支標籤(通用功能);只在 user 要求時 commit/push。

## 6. 安全與資料處理(軍方場景,不可省)

- **音訊零落地**:PCM 只在 gateway/decoder 記憶體流轉,不寫檔、不進 log、不進 DB。
  log 只記 metadata(utt id、時長、延遲),**不記辨識文字內容**(對話機敏)。
- decoder 的 `X-Token` 與 gateway 的 JWT 驗證缺一不可;token 全走 env fail-loud,
  不進 repo(PUBLIC repo,祕密零外洩)。
- **外部版純 http = 語音明文過內網(書面風險接受項,審查補)**:`X-Token` 只防
  「未授權使用 decoder」,**不防竊聽**。外部版部署時,使用者語音以明文 PCM 走
  內網 http 到 GPU 主機 —— 語音內容等同對話內容(機敏)。anila-agent 純 http
  的前例傳的是文字 prompt,這裡是完整語音,風險量級不同,不能只當慣例沿用。
  規則:
  - **內部版(語音不出主機、走 anila-models-net)是內網優先選項**;
  - 只有 GPU 不在平台主機時才用外部版,且部署文件必須列出補償控制
    (GPU 主機防火牆來源白名單只放平台主機 IP、交換器層隔離/專用 VLAN),
    並由資安權責人**書面簽核**這條風險接受 —— 不是埋在 compose 註解裡的一句話。
- 不弱化 SSRF guard/卡登驗章/JWT 信任錨(本功能完全不碰該三者程式碼)。
  Permissions-Policy 的 microphone 放寬(§2.4)是本功能唯一的安全姿態調整,
  範圍限縮在平台聊天 UI 的 server block,隔離 origin 與 uploads 不動。
- Roadmap gate 對位:本功能屬新增能力,不影響既有 Gate 排序;**機敏資料 production
  No-Go 照舊**,ASR 不改變任何資料門檻結論。上線部署前依 roadmap §6.1 走核准。

## 7. Air-gap 打包備忘

- faster-whisper 權重:HuggingFace `Systran/faster-whisper-<size>`(CTranslate2 格式)
  預先下載 → 進 offline bundle → volume 掛到 `ASR_MODEL_DIR`。
- **image 供應鏈(審查補,漏了會在內網炸)**:decoder/gateway 的 Dockerfile 在
  build 時需要外網(`apt-get update`、`pip install`)。air-gap 主機上若 image 沒
  先載入,`docker compose up -d` 會 fallback 去 build,然後卡死在 apt/pip ——
  operator 得到一個跟 ASR 毫無關係的錯誤畫面。流程必須是:
  **外網機 build → `docker save` → 帶進內網 → `docker load` → `up -d --no-build`**。
  standalone 的 README 與部署文件照這個順序寫,不要只寫 `up -d`。
- ⚠ **`ASR_LOCAL_FILES_ONLY` 程式預設是 `false`(fail-open 到網路)**:
  `docker-compose.standalone.yml` 已補成 1,但**內部版的 compose 條目(M4 才寫)
  也必須顯式設 1**。漏設的症狀:權重缺失時卡在 HuggingFace 的 DNS timeout、
  `/health` 永遠 503,查半天以為是 GPU 問題。
- pip wheels:faster-whisper、ctranslate2、**webrtcvad-wheels**、numpy、httpx、
  python-jose、redis、cachetools ——進 wheelhouse。(opencc 預設不需要,見 §10。)
- ⚠ **VAD 套件必須用 `webrtcvad-wheels`,不是原版 `webrtcvad`**(2026-07-17 實測):
  原版 2.0.10 已停止維護,import 第一行就 `import pkg_resources`,而 setuptools 81+
  已移除 pkg_resources → 現代環境 import 直接 `ModuleNotFoundError`(實測 setuptools
  83.0.0 重現)。`webrtcvad-wheels` 2.0.14 拿掉該 import,且提供預編譯 wheel
  (air-gap 不必在內網編 C extension,是加分)。
- **不用 silero VAD**(那要另下載 onnx;webrtcvad 純本地無外抓)。
- `vad_filter=False`(gateway 已切好句,decoder 不再開 faster-whisper 內建 VAD——
  它會觸發 silero 下載,air-gap 直接炸)。

## 8. 決策紀錄

### 8.1 待 user 拍板(審查 2026-07-17 提出;**未定案,實作者勿自行決定**)

1. **decoder 的 GPU 卡位**:本機 dev 的 anila-models stack **四張卡都有主**
   (0=nv-embed、1+2=flux、2=gpt-oss-20b、3=gemma4 且
   `--gpu-memory-utilization 0.97`)——「釘一張空卡」在本機不存在。跟 gemma4
   同卡必 OOM(0.97 沒留餘裕);可擠的只有 kv_cache 0.5 的 gpt-oss 卡(2)或
   embedding 卡(0),但那就是與人共用,與「不要跟 LLM 共用同一張」的指引衝突。
   **M4 改 `infra/models/docker-compose.yml` 前必須先拍板 device_ids。**
   另:內網平台主機 `.15` 是否有 GPU **未查證** —— 若無,內網實際上**只有
   外部版一途**(M7 的「兩種接法各驗一次」在內網只驗得了外部版),部署文件
   重心要跟著調。**建議**(僅供拍板參考):先查 `.15` 的 `nvidia-smi` 再定
   內網型態;本機 dev 暫釘卡 0(embedding 負載間歇、無 0.97 佔滿問題)並實測
   互擾。
2. **併發單位與負載旋鈕**:§2.1 的「十人級併發」指**同時在線 session**,不是
   **同時講話的人**。每個 active speaker 每 0.5s 產生一次 partial 解碼,單
   model + lock 序列化下(V100/medium 對 15s 窗約 0.5–1.5s/次),**2–3 人同時
   講話就會飽和**,再疊上「棄單不取消」(§2.2 編排規則第 4 條)會放大排隊。
   `ASR_PARTIALS_ENABLED=0`(只出 final、犧牲即時預覽)是第一個負載旋鈕。
   **請拍板**:正式部署是否預設關 partial,或等目標 GPU 實測數據再定。

### 8.2 已拍板(2026-07-17,user 確認)

1. **UI:兩個都做**。順序 anilalm 先(M5)、anila-shell 後(M6),邏輯共用。
2. **decoder:內部版 + 外部版兩套部署設定都交付**(同一份碼,見 §2.1 部署位置;
   內網以內部版為優先選項,外部版須簽核 §6 的風險接受項)。
3. **模型尺寸:延後**(env 可換)。GPU 餘裕問到後定案;V100 → `float16`。
4. 串流(非錄完再送)、獨立容器、錄完可編輯再送出:前輪討論已定。
5. **通用型 ASR,無特定領域需求**(2026-07-17)。不加 glossary / hotwords /
   數字正規化;`voice_dataset` 的領域調校數據不適用(見 §10 結論 4)。

## 9. 落地後的分支同步(功能驗完才做)

`main` 合入 → 依 `AGENTS.md` §3.5 順序 cherry-pick downstream;**不覆蓋各分支
`.env.example` 旗標姿態**,ASR 區塊以「預設關」姿態新增;`trial-military` 挑選式
port(本功能不碰它刪掉的檔,預期無 modify/delete 衝突,但仍要驗)。

nginx 的兩處改動(`/asr/` location、Permissions-Policy microphone 放寬 + 註解
改寫,§2.4)屬通用 infra 改動,隨 main cherry-pick 散七分支 —— 特別注意
Permissions-Policy 那行:漏同步的分支上,ASR 按鈕會渲染但麥克風永遠拿不到,
且症狀跟使用者拒絕權限無法區分(§2.4),排查表要記這條。

## 10. 實測證據:OpenCC 不做、initial_prompt 要做(2026-07-17)

資料來源:`~/Desktop/tenya/語料庫/voice_dataset` —— **無人機指揮管制中文語料庫**
(4 位台灣說話者 × 274 句 = 1096 句,約 97 分鐘,附 ground truth 文本 `UAV-1/2.txt`,
領域正好就是 ANILA 的目標場景)。前人已跑過 medium 的有/無 initial_prompt 對照,
本輪重新分析其 `transcripts.csv` 的 **`raw_hypothesis`**(模型原始輸出;`hypothesis`
欄已被 glossary 後處理過 77/1096 句,不能用)。

### 結論 1:whisper medium 在本語料**完全沒有吐簡體**

以 `difflib` 對齊 reference/raw_hypothesis 取出替換錯誤,再判定「hypothesis 的字
是否恰為 reference 那字的簡體」(`t2s(ref_char) == hyp_char`):

| | 替換錯誤總數 | 其中繁→簡 |
|---|---|---|
| medium 無 prompt | 541 | **0 (0.00%)** |
| medium 有 prompt | 536 | **0 (0.00%)** |

### 結論 2:OpenCC 反而會**弄壞**正確的繁體

對 ground truth(台灣人寫的純正繁體,理想情況應該一個字都不動)量誤傷:

| 模式 | 改動句數 | 誤傷內容 |
|---|---|---|
| `s2t` | 108/1096 = 9.85% | `群→羣`(古僻異體)、`干→幹`(**`GNSS 干擾`→`幹擾`,純粹錯誤**) |
| `s2tw` | 68/1096 = 6.20% | `干→幹` |
| `s2twp` | 116/1096 = **10.58%** | `類型→型別`(程式領域用語,套進軍事語境是災難) |

**本文件初版推薦的 `s2twp` 是三者中最糟的。** 在一個 0% 簡體的輸出上套一個
10.58% 誤傷率的轉換,是淨損失 → **預設關閉**。

⚠ 有效範圍(不要過度外推):本結論建立在 **medium** + 這批 **朗讀語音**(TTS
fine-tune 用的乾淨錄音,非自發口語)+ 台灣口音 + 無人機領域。**large-v3 未測**;
它訓練資料更多,繁簡傾向可能不同。改用 large-v3 時**必須重跑這組量測**再決定
`ASR_OPENCC_MODE`。量測腳本邏輯見本節,重跑成本約 10 分鐘。

### 結論 3:initial_prompt 有效,保留

| 實驗 | CER | WER |
|---|---|---|
| medium 無 prompt | 2.74% | 2.66% |
| medium 有 prompt | **1.71%** | **1.80%** |

CER 相對降低 38%。既然 prompt 有效而簡體不是問題,**prompt 的價值在別處**
(領域語感、標點、數字風格),不是繁化。

### 結論 4:真正的錯誤大宗是**數字格式**,但**不處理**

替換錯誤中 39%(無 prompt)～49%(有 prompt)與數字有關:模型寫「五十架」、
ground truth 寫「50架」(`1→一`×55、`2→兩`×38、`3→三`×34…)。

⚠ **這個 ASR 是通用型的,沒有特定領域需求**(2026-07-17 user 明確指示)。所以:

- **不加 glossary、不加 hotwords、不做數字正規化**,無論領域。與內網參考實作
  同調(`transcriber.py:9` "General purpose: no hotwords, no glossary, no number
  normalization")。
- ⚠ **`voice_dataset` 裡領域相關的實驗數據不適用於本專案,不要拿來當目標。**
  該語料庫的 `asr_experiments_3` 顯示 baseline CER 6.1% → `hotwords_glossary`
  1.9%,那是**靠無人機領域詞典換來的**;通用 ASR 沒有那本詞典,也不該有。
  同理 `hotwords_terms.txt` / `glossary_terms.csv` / `make_sherpa_hotwords.py`
  都**不是**本專案的參考來源。
- 本節可用的只有**與領域無關**的兩條:繁簡行為(結論 1、2)與通用 prompt
  「以下是繁體中文。」的效果(結論 3)。

數字格式因此就維持模型原樣。這在本功能的使用情境下不是問題:ASR 產出是
**填進輸入框給人看、給人改**,不是直接執行的指令 ——「五十架」使用者讀得懂,
看一眼就送出。只有當 ASR 之後被用於結構化指令時,數字正規化才會變成硬需求,
那時才重啟討論。

## 11. 已知缺口:幻覺過濾擋不掉短重複(2026-07-17 實測)

M1 實跑時對純 440Hz 正弦波,whisper 吐出 `《寧域》` 與 `詞曲 曲曲 曲曲 曲曲 曲曲`,
且 `no_speech_prob≈0.47` 低於 0.5 門檻,whisper 自己擋不掉。核對參考專案的三道過濾:

- `HALLUCINATION_ALWAYS_RE` — 不含這些詞,不中。
- `HALLUCINATION_SHORT_RE` — 只認「音樂/字幕/music/subtitle」,不中。
- `_looks_degenerate` — **前提是 `len(data) >= 40` bytes 才啟動壓縮比檢查,而
  `詞曲 曲曲 曲曲 曲曲 曲曲` 只有 31 bytes → 直接放行**。

所以參考實作擋不掉這個字串。另外此行為**不穩定**:同一段音訊、同為 final 參數,
直接呼叫吐 `詞曲…`、走 HTTP 卻回空字串 —— 成因是 whisper 的 temperature fallback
(門檻沒過時以遞增 temperature 重試,引入隨機性)。

**但先不要為此改參數。** 純正弦波不是真實輸入:真實幻覺發生在靜音與環境噪音,
而 gateway 還有 webrtcvad + RMS(`MIN_RMS`/`START_RMS`)兩道閘門在前面,純音幾乎
不會被判成語音。參考清單是在真實內網用出來的。**M2 照原樣移植,然後用
`voice_dataset` 的真實語音 + 真實靜音段驗**;確有短重複漏網再調 `_looks_degenerate`
的 40-byte 下限(或加短字串的 n-gram 重複偵測)。對著合成訊號調參數 = 對測試過擬合。

---

## 13. M4 接線紀錄(2026-07-17)

### 已拍板(user 2026-07-17):內網有 H100,模型用 large-v3

`model-serve.sh` 的註解證實內網是 **H100 組**(gemma4/26b-a4b/12b/120b/nv-embed)。
large-v3 fp16 只吃 ~4.7 GiB,對 H100 是零頭 → **§8.1 的「GPU 卡位」待決問題結案**。

### 卡位不寫死:沿用 repo 既有慣例

`infra/models/docker-compose.yml` 的 intranet 組**早就用 env 驅動卡號**
(`${GEMMA_A4B_GPU:-1}`、`${GPT_OSS_120B_GPU:-3}`)。asr-decoder 照做:
`device_ids: ["${ASR_GPU:-0}"]` —— 部署時由 operator 依實際主機配置設定,
規劃書不必也不該預先決定。

### 實際改動

| 檔案 | 改了什麼 |
|---|---|
| `infra/models/docker-compose.yml` | 新增 `asr-decoder`(`profiles: ["intranet"]`、`${ASR_GPU:-0}`、air-gap 三件套 + `ASR_LOCAL_FILES_ONLY=1`、權重唯讀掛載) |
| `infra/deployment/intranet/download-intranet-models.sh` | `MODELS` 加 `Systran/faster-whisper-large-v3`(3.1 GiB);`TOTAL_GIB` 2469 → 2473 |
| `infra/deployment/intranet/model-serve.sh` | `GROUP_INTRANET` 加 `asr-decoder` |
| `infra/compose/platform.yml` | 新增 `asr-gateway`(`profiles: ["asr"]`、locked image ID、`networks: [default, anila-models-net]`) |
| `infra/nginx/anila.conf`、`anila-gate2-pilot.conf` | 平台 server block 放寬 `microphone=(self)`;新增變數式 `/asr/` location |
| `.env.example` | ASR 區塊(9 個變數) |

### 實測驗證(不是看 diff)

- **變數式 proxy_pass 的必要性用實驗證明**:同樣是 upstream 不存在,
  寫死 → `[emerg] host not found in upstream` **nginx 拒絕啟動**;
  變數式 → `test is successful`。地雷是真的,修法有效。
- **兩個 conf 都通過 `nginx -t`**(掛真 snippet + 自簽憑證 + add-host 讓
  upstream 解析得到)。
- **`microphone=(self)` 只出現在平台的 443/4443 兩個 server block**;
  `/uploads/`、`/uploads/flux/` 與 n8n/gitlab/code 三個工具站共 7 處維持 `()`。
- **profile 隔離確認**:不開 profile 時 `asr-gateway` 不在服務清單(七分支
  零影響),`COMPOSE_PROFILES=asr` 才出現。

### ⚠ M4 抓到的 regression:http 放行旗標的語意錯了

`_validate_settings` 早期版本對**任何** `http://` 都要求 `ASR_ALLOW_HTTP_DECODER=1`
→ **platform.yml 的內部版預設值(`http://asr-decoder:9000`)會讓 gateway 直接
起不來**。是 `docker compose config` 解出真實預設值時才發現的。

正確語意:**旗標管的是「語音會不會明文離開這台主機」,不是「有沒有用 https」。**
- `http://asr-decoder:9000`(單標籤服務名,只在 docker 內部 DNS 解得開,
  出不了 compose network)→ **不需要旗標**。
- `http://gpu-host.ai.ncsist.org.tw:9000` / `http://10.53.100.12:9000`
  (FQDN 或 IP → 跨主機)→ **明文過內網,必須顯式放行**。

若不區分,operator 會被迫在內部版也把旗標打開,於是「習慣性打開」反而弱化了
外部版那道真正該守的關卡。判斷邏輯在 `_is_internal_service_name()`。

---

## 12. 技術債:認證模組是 anila-studio 的副本(2026-07-17,M3 產生)

`services/asr-gateway/app/services/jwks_client.py` 與 `revocation_cache.py` 是
`services/anila-studio/app/services/` 同名檔的**逐位元組副本**(vendor 時只加了
標頭,合計約 968 行)。兩個檔案開頭都有 VENDORED 警告。

**為什麼是副本**:抽到 `packages/anila-core` 共用才是正解,但那要改 anila-studio
—— 一個已部署、鎖 image content ID、橫跨七條分支的服務。在「新增語音輸入」的
PR 裡做跨服務的安全模組重構,風險與 review 成本都不成比例。

**這筆債的真實代價**:兩份分歧 = 兩個服務對「什麼是有效權杖」有不同認定,
安全修補只補到一邊。**動到 studio 的這兩個檔時必須同步 gateway,反之亦然。**

**償還建議**(獨立於 ASR 排期,不要夾在 ASR 的 PR 裡):抽到
`packages/anila-core`,studio 與 gateway 同時改用;因為是 JWT 信任錨,要走
完整的安全 review 與七分支同步。

副本能原樣運作的唯一前提:`app/config.py` 的欄位名與 studio 逐字一致
(`CSP_BASE_URL`、`CSP_SERVICE_TOKEN`、`JWKS_REFRESH_SECONDS`、
`REVOCATION_CACHE_TTL_SECONDS`、`REVOCATION_RECONCILE_INTERVAL_SECONDS`、
`INTERNAL_TIMEOUT_*`)。改名 = 副本壞掉,且要到 runtime 才炸。

### M3 實作時對規格的兩處補充

1. **close code 表少了一格**:規劃書 §2.2 只定義 4401/4408/4409,但
   fail-closed(JWKS 拉不到、撤銷清單未就緒)不屬於任何一格。回 4401 會叫
   使用者「重新登入」——但問題出在 Redis/csp,重登解決不了。已新增
   **4503 = 驗證基礎設施不可用**,`auth.py` 以 `AuthError`(→4401)與
   `AuthUnavailable`(→4503)兩種例外區分。
2. **ping/pong 不自己做**:uvicorn 的 websockets 實作已內建 keepalive ping
   (`--ws-ping-interval`,Dockerfile 設 20s),死連線由它回收。`_guard` 只管
   session 上限與撤銷重查。`ASR_PING_INTERVAL_SECONDS` 是給部署設 uvicorn
   旗標用的,程式不讀它。
