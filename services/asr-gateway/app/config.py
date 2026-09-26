"""asr-gateway runtime settings.

JWT / Redis / CSP 那組刻意與 anila-studio 同名同義(見 platform.yml 的
anila-studio 區塊)—— 跨服務認證照抄它的結構,env 名稱漂移只會製造混亂。
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service identity
    APP_NAME: str = "asr-gateway"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"
    ANILA_DEPLOYMENT_PROFILE: str = "development"

    # ── decoder ─────────────────────────────────────────────────────────
    # gateway 出向只允許這一個 URL(或治理中心指派的那一個)。兩個採用點都
    # 過 anila_core 的 SSRF guard(`validate_outbound_url`,endpoint_kind
    # ='model')—— 遠端解碼一旦成立,ASR 就是平台唯一會跳過那道門的模型呼叫。
    ASR_DECODE_URL: str = ""
    # native 協定的共享祕密,送成 `X-Token`(本地 services/asr-decoder)。
    ASR_DECODER_TOKEN: str = ""
    # ── 傳輸協定 ────────────────────────────────────────────────────────
    # 'native' = ANILA 自有契約(裸 PCM + X-Token,本地 decoder / 氣隙 bundle)
    # 'openai' = POST {base}/v1/audio/transcriptions(multipart WAV + Bearer)
    # ⚠ 認不得的值**開不了機**(app/main.py:_validate_settings)。選錯協定的
    # 症狀是每句話都 404/401 而麥克風看起來正常 —— 這種「設了、沒報錯、其實
    # 沒生效」的靜默錯誤,本專案已經有一整份紀錄(docs/FAKE-CONTROLS.md)。
    ASR_DECODE_PROTOCOL: str = "native"
    # openai 協定的 Bearer 金鑰(治理中心沒替該端點掛金鑰時的環境變數退路)。
    # ⚠ 祕密:不進 log、不進 /asr/health、不進錯誤訊息。
    ASR_DECODE_API_KEY: str = ""
    # multipart 的 `model` 欄位。OpenAI 相容端點一律必填,名稱由對方決定。
    ASR_OPENAI_MODEL: str = "whisper-1"
    # How often asr-gateway re-reads CSP's asr-primary designation (seconds).
    # Read via Settings (not os.environ at import) so tests and operators share
    # one knobs surface with the rest of this service.
    ASR_DECODE_URL_TTL: float = 60.0
    # ── 健康探針 timeout ────────────────────────────────────────────────
    # 舊值是 2.0s 總計 / 1.0s 連線 —— 那是「decoder 就在同一台的容器裡」調出來
    # 的。解碼端一旦在 WAN 另一頭(或是會冷啟動的算力中心端點),2 秒會回報
    # decoder_unreachable 並把一支**其實能用**的麥克風藏起來。
    # 連線 3.0s 對齊本檔既有的 INTERNAL_TIMEOUT_CONNECT;總計 8.0s 容得下一次
    # 冷啟動,又仍遠低於 final 解碼的 30s(探針不該比真正的工作還能等)。
    ASR_PROBE_CONNECT_TIMEOUT_SECONDS: float = 3.0
    ASR_PROBE_TIMEOUT_SECONDS: float = 8.0

    # ── 辨識 ────────────────────────────────────────────────────────────
    # ⚠ 這個 prompt 是通用的,不是領域詞典。實測(規劃書 §10)顯示它讓 CER
    # 從 2.74% 降到 1.71%,但價值在語感/標點/數字風格,不在繁化 —— whisper
    # 在真實繁中語料的簡體率本來就是 0%。
    ASR_INITIAL_PROMPT: str = "以下是繁體中文。"
    ASR_BEAM_SIZE: int = 5
    # 負載過高時的第一個旋鈕:關掉 partial,只留 final(見規劃書 §8.1)。
    ASR_PARTIALS_ENABLED: bool = True
    ASR_VAD_AGGRESSIVENESS: int = 2

    # ⚠ 預設 off,且不要改成 s2twp。實測:whisper medium 在 1096 句真實繁中
    # 語料的簡體率 0%,而 OpenCC 對正確繁體的誤傷率 s2t 9.85% / s2tw 6.20% /
    # s2twp 10.58%(s2twp 會把「類型」轉成「型別」)。見規劃書 §10。
    # 改用 large-v3 後若實測出現簡體才考慮開,開之前先重跑 §10 的誤傷量測。
    # 非 off 時才 import opencc(它不在 runtime 相依裡 → 沒裝就 ImportError,
    # 這是刻意的 fail-loud,不要加 try/except 吞掉)。
    ASR_OPENCC_MODE: str = "off"

    # ── 資源上限 ────────────────────────────────────────────────────────
    ASR_MAX_SESSION_SECONDS: int = 300
    # 逾時收尾時等最後一個 final 的上限。等太久等於沒有逾時。
    ASR_FLUSH_GRACE_SECONDS: float = 5.0
    # 16kHz × 2 bytes = 32 KB/s 是正常速率。超過代表對方在灌流量。
    ASR_MAX_INGRESS_BYTES_PER_SEC: int = 96_000
    ASR_PING_INTERVAL_SECONDS: float = 20.0

    # ── 認證 ────────────────────────────────────────────────────────────
    # ⚠ 以下欄位名**必須與 anila-studio 逐字一致**:app/services/ 底下的
    # jwks_client.py 與 revocation_cache.py 是 studio 的逐位元組副本(見那兩
    # 個檔的 VENDORED 標頭),它們直接讀 settings.<NAME>。改名 = 副本壞掉,
    # 而且要到 runtime 才炸。新增欄位請放到本區塊之外。
    CSP_BASE_URL: str = "http://csp:8000"
    # revocation cache 冷啟動要拿它以 X-CSP-Service-Token 打 csp 的
    # /api/auth/revocations 做全量同步。漏了它 cache 永遠不 ready,fail-closed
    # 之下所有 WS 一律被拒,而症狀長得像 auth 壞掉。
    CSP_SERVICE_TOKEN: str = ""
    REDIS_URL: str = "redis://redis:6379/0"
    REDIS_REVOCATION_CHANNEL: str = "anila:auth:token-revoke"
    JWT_KID: str = "anila-v1"
    JWT_ALGORITHMS: tuple[str, ...] = ("RS256",)
    # ⚠ 這裡刻意**沒有** JWT_ISSUER / JWT_AUDIENCE。csp 的 create_access_token
    # 不簽 `iss` 也不簽 `aud`(services/csp/app/utils/security.py:208),studio
    # 的 Settings 同樣沒這兩個欄位。留著它們(哪怕只是「有設才驗」)會直接復活
    # 2026-07-31 的故障:platform.yml 有給 JWT_ISSUER/JWT_AUDIENCE 值,「有設才
    # 驗」在部署環境等於「一律驗」→ 一律 4401。要驗 iss/aud 的前提是 csp 先開始
    # 簽,那時再一起加回來。多出來的 env var 不會讓 Settings 爆掉(pydantic-
    # settings 只對顯式 kwargs forbid extra),所以 compose 不需要同步改。
    JWT_LEEWAY_SECONDS: int = 60
    JWKS_REFRESH_SECONDS: int = 3600
    # 與 studio 的 jwks_client 副本一起讀。未知 kid 強制重抓的最短間隔。
    JWKS_UNKNOWN_KID_REFETCH_SECONDS: int = 30
    REVOCATION_CACHE_TTL_SECONDS: int = 30 * 24 * 3600
    REVOCATION_RECONCILE_INTERVAL_SECONDS: int = 5
    INTERNAL_TIMEOUT_CONNECT: float = 3.0
    INTERNAL_TIMEOUT_SECONDS: float = 10.0
    # ⚠ 這裡刻意**沒有** COOKIE_SECURE / CSP 的 ANILA_AUTH_MODE(理由見
    # app/auth.py 檔頭與 authenticate() 內的註解):
    # - COOKIE_SECURE 以前用來在 `__Host-anila_access_token` 與
    #   `anila_dev_access_token` 之間選 cookie 名,但兩個名字平台上都沒人發。
    #   cookie 名現在是常數 `anila_access_token`(csp 唯一會發的那個)。
    # - CSP 的 card-only 模式以前被誤解成要在權杖內附 `amr="sc"`,而 csp 從不簽
    #   `amr` → 在這裡重複判斷會連卡登入的人都拒絕。「只准卡登入」的執法點在 csp 的
    #   登入端點,不在這裡。

    # ── 長連線特有(studio 沒有,因為它是 per-request)──────────────────
    # 握手驗過之後,session loop 每 N 秒對 in-memory revocation cache 重查一次
    # (cache 本來就靠 Redis channel 即時更新,重查只是本地查表、零 I/O)。
    # 不這樣做的話,長連線在握手後就再也不看撤銷了。
    REVOCATION_RECHECK_SECONDS: float = 30.0


settings = Settings()
