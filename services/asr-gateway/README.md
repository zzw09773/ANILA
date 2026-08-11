# asr-gateway

串流語音輸入的 WebSocket 端點。瀏覽器麥克風 → 切句 → 打 `asr-decoder` 解碼 →
把文字推回前端。**純 CPU、無 GPU、無 DB。**

整體架構、WS 協定、實測證據、部署步驟見
`docs/planning/asr-voice-input-plan.md` 與 `docs/runbooks/asr-voice-input.md`。

## 職責邊界

| 在 gateway | 在 decoder(`services/asr-decoder`) |
|---|---|
| WS 連線、JWT 驗證、per-user 併發 | faster-whisper 解碼 |
| webrtcvad 切句、partial/final 節奏 | 無狀態:收 PCM、回文字 |
| 協定不變式、繁中後處理旗標 | GPU |

decoder 完全不知道 session 的存在;所有「時序」都在 gateway。

## 端點

⚠ **路由自帶 `/asr` 前綴** —— nginx 的 `location /asr/` 不 strip prefix。
打 `/health`(少了 `/asr`)會 404,那是路徑錯,不是服務掛了。

```
WS  /asr/stream    主通道
GET /asr/health    200 + status=ok = 麥克風可顯示(撤銷 ready 且 decoder 探針通過)
                   503 + status=degraded/unavailable = 隱藏麥克風;
                   reason 區分 voice off(csp_unreachable)與指到壞位址(decoder_*)
```

### WS 協定

client → server:
- binary frame = Int16 mono PCM @16kHz
- text frame = `{"type":"flush"}`(使用者按停,強制收尾當前語句)

server → client(皆 JSON text frame):
- `{"type":"status","state":"listening|recording",...}`
- `{"type":"partial","id","text"}` — 同 id 整段取代
- `{"type":"final","id","text","latency_seconds",...}`
- `{"type":"discard","id"}` — 太短/太小聲/幻覺,前端清掉該 id 的 partial
- `{"type":"error","msg"}`

**協定不變式**:同一 id 的 `final`/`discard` 送出後,不再送該 id 的 `partial`;
前端收到違反的 partial 一律忽略(雙保險)。少了它,遲到的 stale partial 會在
定稿後讓預覽文字復活且永遠清不掉。

### close code

| code | 意義 | 前端該做的事 |
|---|---|---|
| 4401 | 權杖無效/過期/被撤銷 | 提示重新登入 |
| 4408 | session 逾時(先 flush 再關,不丟尾句) | 提示「再按一次繼續」 |
| 4409 | 同一 user 開了新連線,舊的被踢 | 提示「已被其他分頁/裝置接手」 |
| 4503 | 驗證基礎設施不可用(JWKS/撤銷清單) | 提示稍後再試(重登無用) |

⚠ close code 要送得到前端,**必須先 `accept()` 再 `close(code)`** —— Starlette
對「未 accept 就 close」的 WS 回 HTTP 403,前端只看得到 1006。

## 認證

結構逐條移植 anila-studio(RS256 + JWKS 本地驗章、session envelope 檢查、拒絕
refresh token、fail-closed 撤銷查核)。長連線特有的兩點:

- **cookie 名固定**:`anila_access_token`。平台的 CSP 只發這一個 cookie；瀏覽器的
  WebSocket API 不能帶 Authorization header → 對瀏覽器 cookie 是唯一路徑,名字錯
  就是全部 4401。
- **撤銷長連線重查**:握手驗過後,每 `REVOCATION_RECHECK_SECONDS`(預設 30s)
  對 in-memory 撤銷 cache 重查一次;查到 revoked → close 4401。token 過期則
  是明確的接受決策(握手驗一次,session 上限 300s ≪ access token 60 分鐘)。

⚠ **`app/services/jwks_client.py` 與 `revocation_cache.py` 是 anila-studio 的
vendored 副本**(檔頭有 VENDORED 警告)。改動必須同步 studio 那份,反之亦然 ——
兩份分歧 = 兩個服務對「什麼是有效權杖」認定不同。見規劃書 §12。

## 併發與資源

- **每 user 最多 1 條 session,踢舊留新**(不是拒新):筆電闔蓋/斷網留下的死
  TCP gateway 察覺不到,拒新會把使用者鎖在門外。第二條連上 → 舊的收 4409。
- ⚠ **必須 `--workers 1`**(Dockerfile 已設):per-user 計數在記憶體,多 worker
  會讓每個 process 各自計數,限制失效、踢舊也踢不到別的 process。要 scale 得先
  把計數搬到 Redis。gateway 無狀態、CPU 輕,單 worker 夠用。
- 死連線靠 uvicorn 內建 keepalive ping 回收(`--ws-ping-interval`),不自己送。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `ASR_DECODE_URL` | (必填) | 解碼端位置。本地 `http://asr-decoder:9000`;外部 `https://<host>[/v1]`。CSP 無 asr-primary 時的 fallback。**會過 SSRF guard**(見下)。 |
| `ASR_DECODE_PROTOCOL` | `native` | `native`(裸 PCM + `X-Token`)或 `openai`(multipart WAV + `Bearer`)。**值不合法直接開不了機**。 |
| `ASR_DECODER_TOKEN` | native 必填 | 與本地 decoder 共享的密鑰,兩邊同值。換機器時新機必須部署同一 token,否則 `/asr/health` 回 `decoder_unauthorized`。 |
| `ASR_DECODE_API_KEY` | openai 必填 | 送成 `Authorization: Bearer`。治理中心那筆 asr 模型若自帶 `api_key` 以它優先。⚠ 祕密:不進 log / health / 錯誤訊息。 |
| `ASR_OPENAI_MODEL` | `whisper-1` | multipart 的 `model` 欄位,名稱由對方端點決定。 |
| `ASR_DECODE_URL_TTL` | `60` | 重讀 CSP asr-primary 的間隔(秒)。走 pydantic Settings,不是 import 時讀 `os.environ`。 |
| `ASR_PROBE_TIMEOUT_SECONDS` | `8` | `/asr/health` 探針的總 timeout。舊值寫死 2.0s(同機時代),跨 WAN 會誤報 `decoder_unreachable` 並藏掉一支能用的麥克風。 |
| `ASR_PROBE_CONNECT_TIMEOUT_SECONDS` | `3` | 同上,連線階段。對齊 `INTERNAL_TIMEOUT_CONNECT`。 |
| `ANILA_ALLOW_HTTP_ENDPOINT` / `ANILA_ALLOW_PRIVATE_ENDPOINT` / `ANILA_TRUSTED_HOSTS` | — | `anila_core` SSRF guard 讀的三個旗標。compose 會自動把 `asr-decoder` 併進本服務的 trusted hosts。 |
| `ASR_INITIAL_PROMPT` | `以下是繁體中文。` | 通用 prompt,非領域詞典。 |
| `ASR_BEAM_SIZE` | `5` | final 解碼的 beam。 |
| `ASR_PARTIALS_ENABLED` | `1` | 負載旋鈕:設 0 只留定稿,GPU 壓力大減。 |
| `ASR_OPENCC_MODE` | `off` | `off`/`s2t`/`s2tw`。⚠ 不要用 s2twp(見規劃書 §10)。 |
| `ASR_MAX_SESSION_SECONDS` | `300` | 單次語音上限;逾時先 flush 再斷。 |
| `CSP_BASE_URL` / `CSP_SERVICE_TOKEN` / `REDIS_URL` / `JWT_*` | — | 與 anila-studio 同名同義。`CSP_SERVICE_TOKEN` 是撤銷 cache 冷啟動同步用,漏了 cache 永遠不 ready → 所有 WS 被拒。 |

> `ASR_ALLOW_HTTP_DECODER` 已於 2026-08-05 **退役**:它從被馴服之後就沒有任何
> 程式在讀,而名字讀起來像一個安全旗標(維運者設 0 會以為自己關掉了 http)。
> 紀錄在 `docs/FAKE-CONTROLS.md` #31。舊 `.env` 留著那一行不會壞。

### 本地與外部,兩條都是一等公民

擁有者的要求原話是「不論是本地還是外部伺服器都要可以連線」。

| | native | openai |
|---|---|---|
| 路徑 | `POST {base}/transcribe` | `POST {base}/v1/audio/transcriptions` |
| body | 無標頭 Int16 mono PCM @16k | multipart,`file` 是 **16k/mono/int16 的 WAV** |
| 認證 | `X-Token: <shared secret>` | `Authorization: Bearer <key>` |
| 用在 | 本地 `services/asr-decoder`、手提氣隙 bundle | 算力中心的 API 端點 |
| 健康探針 | `GET /health` + 帶祕密的 `/transcribe` | 100 ms 靜音的真實辨識請求 |

⚠ **WAV framing 是 gateway 的責任,不是對方猜**。少了那 44 bytes,寬鬆的實作
會照自己的預設取樣率解碼 → 讀得出來但語速全錯的逐字稿(靜默錯誤,比 400 貴)。
見 `app/wav.py`。

⚠ **beam 在 openai 協定表達不了**,`ASR_BEAM_SIZE` 對它無效;`no_speech_prob` /
`avg_logprob` 也拿不到,一律回 0.0 = 「沒有訊號、不要據此丟棄」,幻覺過濾改由
文字面的 `is_hallucination` / `looks_degenerate` 負責。

### 出向檢查(SSRF guard)

解碼位址的**兩個**採用點都過 `anila_core.security.url_guard.validate_outbound_url`
(`endpoint_kind='model'`):啟動時的 `ASR_DECODE_URL`,以及執行中 CSP asr-primary
指派的位址。在此之前**環境變數那條任何一層都沒驗**——解碼端還在同一台機器時
被「operator 自己設的」擋著,一旦位址可以指到院外,它就會是平台唯一跳過 guard
的模型呼叫。

- http 由 `ANILA_ALLOW_HTTP_ENDPOINT` 決定,跟其他 model endpoint **同一個旗標**。
- 本地 `http://asr-decoder:9000` 是單標籤 docker 服務名,guard 一律擋;
  platform.yml 把 `asr-decoder` 併進本服務的 `ANILA_TRUSTED_HOSTS` 放行 ——
  那是 guard 文件寫明給 operator 的機制,**不是把檢查關掉**。
- guard **沒有為了 ASR 放寬任何一條**。迴環 / cloud metadata / link-local 一律擋。
- ⚠ **本地語音因此也依賴 `ANILA_ALLOW_HTTP_ENDPOINT=1`。** 把它收成 `0` 的站台
  會在**啟動時**被 reason=`scheme` 擋下 —— `ANILA_TRUSTED_HOSTS` 救不了,scheme
  檢查排在主機名檢查前面。分診表見 `docs/runbooks/asr-voice-input.md` §3c。
- ⚠ **image 裡的 guard 是 build 時的拷貝,會跟 repo 漂開。** 沒有 bind mount、
  `up -d` 不重建 → 改了 `packages/anila-core/.../url_guard.py` 之後只 `up -d`,
  這個服務仍在跑舊規則,**而且沒有任何錯誤訊息**。指紋在
  `/app/.url_guard.sha256`,比對方式見同一節。

### 解碼端憑證從哪來

1. 治理中心那筆 asr 模型自帶的 `api_key`(加密的 `api_key_secret_ref`)——
   只在**服務權杖**通道上回傳,人類呼叫者永遠拿不到。
2. 沒有的話才用環境變數(`ASR_DECODER_TOKEN` / `ASR_DECODE_API_KEY`,依協定)。

⚠ 刻意**不吃** `MODEL_GATEWAY_API_KEY` 全域退路 —— 不論是「這筆沒掛金鑰」還是
「掛了但**解不開**」。csp 端用的是 `_asr_row_own_key`(直接解信封、失敗回 `None`),
不是 LLM proxy 那支會 fail-soft 退回全域金鑰的 `resolve_model_gateway_key`。
兩個理由:
1. 拿到一把不相干的模型金鑰只會 401,而 401 跟「金鑰設錯」分不出來;
2. 解不開這件事**很現實** —— 輪替 `SECRET_KEY`、或把資料庫還原進另一組金鑰的
   環境,每一筆 ref 會同時解不開。那時 fail-soft 等於把 LLM gateway 的憑證交給
   算力中心的辨識端點,跨了信任邊界。

## 測試

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

解碼與 VAD 都以假件注入(`vad=`、`decode=`、`create_app(skip_upstreams=...)`),
所以測試不需要 GPU、decoder、Redis、csp、網路(anila_core 的 SSRF guard 由
`tests/conftest.py` 把 `packages/anila-core/src` 掛上 sys.path,對齊 image 的做法)。
155 個測試涵蓋:切句狀態機、async 編排七規則(stale partial 丟棄、in-flight
塌縮、逾時 flush、decoder 掛掉不 crash)、WS close code 可達性、併發踢舊留新、
出向檢查的正反兩面、WAV framing、兩種協定的憑證與探針語意。

## 安全

- **音訊零落地**:PCM 只在記憶體流轉,不寫檔、不進 log。log 只記 metadata。
- **不弱化 SSRF/卡登/JWT 信任錨**:gateway 出向只有解碼端一個目的地(環境變數
  或治理中心指派),兩者都過 `validate_outbound_url`;程式內無任何由 client
  決定目的地的請求。
- **祕密不外流**:解碼端憑證不進 log、不進 `/asr/health`、不進送給前端的
  錯誤訊息(`decode_client._redact` / `decode_probe._redact`)。
- 容器 non-root(uid 10001)。
