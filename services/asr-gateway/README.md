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
GET /asr/health    200 = 可用;503 = revocation cache 沒 ready(fail-closed,
                   此時 WS 一律被拒 → health 誠實回 503,不騙 operator)
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

- **cookie 名雙軌**:`COOKIE_SECURE=true` → `__Host-anila_access_token`;
  `false`(本機 dev)→ `anila_dev_access_token`。瀏覽器的 WebSocket API 不能帶
  Authorization header → 對瀏覽器 cookie 是唯一路徑,名字錯就是全部 4401。
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
| `ASR_DECODE_URL` | (必填) | decoder 位置。內部版 `http://asr-decoder:9000`;外部版 `http://<host>:9000`。 |
| `ASR_DECODER_TOKEN` | (必填) | 與 decoder 共享的密鑰,兩邊同值。 |
| `ASR_ALLOW_HTTP_DECODER` | `0` | 外部主機走 http 才需要開(=語音明文過內網,見下)。 |
| `ASR_INITIAL_PROMPT` | `以下是繁體中文。` | 通用 prompt,非領域詞典。 |
| `ASR_BEAM_SIZE` | `5` | final 解碼的 beam。 |
| `ASR_PARTIALS_ENABLED` | `1` | 負載旋鈕:設 0 只留定稿,GPU 壓力大減。 |
| `ASR_OPENCC_MODE` | `off` | `off`/`s2t`/`s2tw`。⚠ 不要用 s2twp(見規劃書 §10)。 |
| `ASR_MAX_SESSION_SECONDS` | `300` | 單次語音上限;逾時先 flush 再斷。 |
| `CSP_BASE_URL` / `CSP_SERVICE_TOKEN` / `REDIS_URL` / `JWT_*` | — | 與 anila-studio 同名同義。`CSP_SERVICE_TOKEN` 是撤銷 cache 冷啟動同步用,漏了 cache 永遠不 ready → 所有 WS 被拒。 |

### `ASR_ALLOW_HTTP_DECODER` 的語意

旗標管的是「**語音會不會明文離開這台主機**」,不是「有沒有用 https」:

- `http://asr-decoder:9000`(單標籤服務名,只在 docker 內部 DNS 解得開,出不了
  compose network)→ **不需要旗標**,音訊不出主機。
- `http://gpu-host.ai.ncsist.org.tw:9000` / `http://10.53.100.12:9000`(FQDN 或
  IP → 跨主機)→ **明文過內網,必須顯式開旗標**。這是書面風險接受項(規劃書 §6)。

不區分的話,operator 會被迫在內部版也開旗標,「習慣性打開」反而弱化了外部版
那道真正該守的關卡。

## 測試

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

解碼與 VAD 都以假件注入(`vad=`、`decode=`、`create_app(skip_upstreams=...)`),
所以測試不需要 GPU、decoder、Redis、csp、網路。78 個測試涵蓋:切句狀態機、
async 編排七規則(stale partial 丟棄、in-flight 塌縮、逾時 flush、decoder 掛掉
不 crash)、WS close code 可達性、併發踢舊留新、http 旗標語意。

## 安全

- **音訊零落地**:PCM 只在記憶體流轉,不寫檔、不進 log。log 只記 metadata。
- **不弱化 SSRF/卡登/JWT 信任錨**:gateway 出向只允許 `ASR_DECODE_URL` 一個目的地,
  程式內無任何由 client 決定目的地的請求。
- 容器 non-root(uid 10001)。
