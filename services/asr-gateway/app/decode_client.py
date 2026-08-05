"""解碼端的 HTTP 客戶端 —— 兩種傳輸都是一等公民。

* **native**(`DecodeClient`)——ANILA 自有契約:`POST {base}/transcribe`,
  body 是無標頭 Int16 mono PCM,認證是 `X-Token` 共享祕密。本地
  `services/asr-decoder` 講這個,手提氣隙 bundle 帶的也是它。
* **openai**(`OpenAIDecodeClient`)——`POST {base}/v1/audio/transcriptions`,
  multipart 上傳 WAV,認證是 `Authorization: Bearer`。算力中心那類外部端點
  講這個。

⚠ **兩條路都要留**:擁有者的原話是「不論是本地還是外部伺服器都要可以連線」。
沒有算力中心的站台只有本地 decoder;有算力中心的站台可能連 GPU 都沒有。
哪一條生效由 `ASR_DECODE_PROTOCOL` 明說,值不認得就開不了機
(`app/main.py:_validate_settings`),而不是預設成某一邊然後每句話都 401。

契約與 timeout 策略沿用內網參考實作(`streaming_asr/app.py` 的
`make_remote_decode`),只把 urllib 換成 httpx(async)。

timeout 的不對稱是刻意的,別「統一」它:
  * partial 每 ~0.5s 就重出一次、壽命極短 → 4s 還沒回來就已經過時,快棄
    比等到好;等下去只會拖累後面的 final。
  * final 會留在畫面上、丟了使用者就永遠看不到那句話 → 30s,等得起忙碌的
    GPU 或冷啟動的模型。
"""

from __future__ import annotations

import logging
import time

import httpx
import numpy as np

from anila_core.security.upstream_urls import join_upstream_path

from app.wav import pcm16_to_wav

logger = logging.getLogger(__name__)

PARTIAL_TIMEOUT_SECONDS = 4.0
FINAL_TIMEOUT_SECONDS = 30.0

KIND_PARTIAL = "partial"
KIND_FINAL = "final"

PROTOCOL_NATIVE = "native"
PROTOCOL_OPENAI = "openai"
SUPPORTED_PROTOCOLS = (PROTOCOL_NATIVE, PROTOCOL_OPENAI)

# OpenAI 相容端點的辨識路徑。版本段交給 join_upstream_path 處理 ——
# 有人把 endpoint 登記成 `https://h/v1`,直接接字串會變成 `/v1/v1/...`。
OPENAI_TRANSCRIPTION_PATH = "/v1/audio/transcriptions"


class DecodeError(RuntimeError):
    """final 解碼失敗。partial 失敗不會走到這裡(靜默丟棄)。"""


def normalise_protocol(raw: str | None) -> str:
    """把 `ASR_DECODE_PROTOCOL` 正規化;認不得就 raise。

    ⚠ **不提供「猜一個」的退路**。這個專案已經有一整份假控制項紀錄,
    共同點都是「設了、沒報錯、實際上沒生效」。傳輸協定選錯的症狀是每一句話
    都 401/404 而麥克風看起來好好的 —— 正是最貴的那種。
    """
    value = (raw or "").strip().lower()
    if value not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"ASR_DECODE_PROTOCOL={raw!r} 不是支援的值;"
            f"只接受 {' / '.join(SUPPORTED_PROTOCOLS)}"
        )
    return value


def _pcm_bytes(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _redact(text: str, *secrets: str) -> str:
    """把祕密從要外流的字串裡抹掉。

    ⚠ repo 是 PUBLIC,而這串字會進 log、進 `{"type":"error"}` 送給前端。
    httpx 的例外訊息本身不含 header,但 URL 可能被貼上 userinfo,回應 body
    也可能把金鑰回音出來 —— 一律先過這裡。
    """
    result = text
    for secret in secrets:
        token = (secret or "").strip()
        if len(token) >= 4:
            result = result.replace(token, "<redacted>")
    return result


class _BaseDecodeClient:
    """兩種傳輸共用的骨架:位址/憑證可熱抽換、失敗策略一致。

    位址與憑證都是**執行中可換**的:治理中心改了 asr-primary,
    `decode_endpoint.refresh_decode_endpoint` 會直接套到現有 client 上,
    不重建連線池、不重啟服務。
    """

    protocol: str = PROTOCOL_NATIVE

    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._credential = credential or ""
        self._client = client or httpx.AsyncClient()

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_base_url(self, base_url: str) -> None:
        """Point at a new decoder without recreating the client (TTL refresh)."""
        self._base_url = (base_url or "").rstrip("/")

    def set_credential(self, credential: str | None) -> None:
        """換憑證(治理中心指派的端點可以自帶金鑰)。

        ⚠ 不記 log、不入 repr —— 這是祕密,不是設定。
        """
        self._credential = credential or ""

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decode(
        self,
        samples: np.ndarray,
        *,
        kind: str,
        prompt: str | None,
        beam: int,
        language: str = "zh",
    ) -> dict:
        """回 {"text","no_speech_prob","avg_logprob","decode_seconds"}。

        partial 失敗 → 回 {"text": ""}(反正下一個 partial 馬上就來)。
        final 失敗 → raise DecodeError(呼叫端負責回報使用者)。
        """
        pcm = _pcm_bytes(samples)
        timeout = PARTIAL_TIMEOUT_SECONDS if kind == KIND_PARTIAL else FINAL_TIMEOUT_SECONDS
        try:
            return await self._post(
                pcm,
                kind=kind,
                prompt=prompt,
                beam=beam,
                language=language,
                timeout=timeout,
            )
        except Exception as exc:
            detail = _redact(f"{type(exc).__name__}: {exc}", self._credential)
            if kind == KIND_PARTIAL:
                # 不記音訊內容,也不吵 —— partial 掉了是預期內的事。
                logger.debug("partial decode dropped: %s", detail)
                return {"text": ""}
            raise DecodeError(f"語音解碼無回應({detail})") from exc

    async def _post(
        self,
        pcm: bytes,
        *,
        kind: str,
        prompt: str | None,
        beam: int,
        language: str,
        timeout: float,
    ) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


class DecodeClient(_BaseDecodeClient):
    """ANILA 原生契約(`services/asr-decoder`)。

    ⚠ 建構子第二個參數歷史上叫 `token`(`ASR_DECODER_TOKEN`),語意是共享
    祕密。改名成 `credential` 只是為了跟 openai 那條共用骨架,送出的 header
    仍然是 `X-Token`。
    """

    protocol = PROTOCOL_NATIVE

    async def _post(
        self,
        pcm: bytes,
        *,
        kind: str,
        prompt: str | None,
        beam: int,
        language: str,
        timeout: float,
    ) -> dict:
        params = {
            "kind": kind,
            "beam": beam,
            "prompt": prompt or "",
            "language": language,
        }
        resp = await self._client.post(
            f"{self._base_url}/transcribe",
            params=params,
            content=pcm,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Token": self._credential,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()


class OpenAIDecodeClient(_BaseDecodeClient):
    """OpenAI 相容的 `/v1/audio/transcriptions`(算力中心那類外部端點)。

    與原生契約的三個實質差異:
      1. **要 WAV 檔頭**。原生送裸 PCM,對方靠部署設定知道 16k/mono/int16;
         multipart 上傳沒有這層約定,取樣率必須寫在檔案裡(`app/wav.py`)。
      2. **認證是 `Authorization: Bearer`**,不是 `X-Token`。
      3. **沒有 beam / no_speech_prob / avg_logprob**。beam 在這個 API 沒有
         對應欄位,直接不送(不是忽略錯誤,是這個協定表達不了);兩個信心
         分數回 0.0 —— `session.py:252` 的丟棄條件要求「兩個指標都難看」,
         0.0 剛好等於「沒有訊號、不要據此丟棄」,幻覺過濾仍由文字面的
         `is_hallucination` / `looks_degenerate` 負責。
    """

    protocol = PROTOCOL_OPENAI

    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url, credential, client=client)
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def transcription_url(self) -> str:
        return join_upstream_path(self._base_url, OPENAI_TRANSCRIPTION_PATH)

    def auth_headers(self) -> dict[str, str]:
        """空憑證時**不送** Authorization —— 送 `Bearer ` 空字串會讓某些
        gateway 回 400 而不是 401,把「沒帶金鑰」偽裝成「請求壞掉」。"""
        if not self._credential:
            return {}
        return {"Authorization": f"Bearer {self._credential}"}

    async def _post(
        self,
        pcm: bytes,
        *,
        kind: str,
        prompt: str | None,
        beam: int,
        language: str,
        timeout: float,
    ) -> dict:
        wav = pcm16_to_wav(pcm)
        data = {
            "model": self._model,
            "response_format": "json",
            "language": language,
            # 0 = 不做 temperature fallback。串流逐字稿要的是穩定,不是多樣性。
            "temperature": "0",
        }
        if prompt:
            data["prompt"] = prompt
        started = time.monotonic()
        resp = await self._client.post(
            self.transcription_url(),
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
            headers=self.auth_headers(),
            timeout=timeout,
        )
        if resp.status_code >= 400:
            # raise_for_status 的訊息只有狀態碼與 URL;上游的錯誤說明在 body,
            # 那才是 operator 需要看到的。截斷 + 抹祕密後往上帶。
            body = _redact(resp.text[:200], self._credential)
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} from transcription endpoint: {body}",
                request=resp.request,
                response=resp,
            )
        payload = resp.json()
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, dict):
            text = payload.get("text") or ""
        else:
            text = ""
        return {
            "text": text,
            "no_speech_prob": 0.0,
            "avg_logprob": 0.0,
            "decode_seconds": time.monotonic() - started,
        }


def env_credential(settings) -> str:
    """該協定在「治理中心沒指派金鑰」時使用的環境變數憑證。"""
    protocol = normalise_protocol(getattr(settings, "ASR_DECODE_PROTOCOL", None))
    if protocol == PROTOCOL_OPENAI:
        return (settings.ASR_DECODE_API_KEY or "").strip()
    return (settings.ASR_DECODER_TOKEN or "").strip()


def make_decode_client(settings, *, client: httpx.AsyncClient | None = None):
    """依 `ASR_DECODE_PROTOCOL` 建對應的傳輸。值不合法直接 raise。"""
    protocol = normalise_protocol(settings.ASR_DECODE_PROTOCOL)
    credential = env_credential(settings)
    if protocol == PROTOCOL_OPENAI:
        return OpenAIDecodeClient(
            settings.ASR_DECODE_URL,
            credential,
            model=settings.ASR_OPENAI_MODEL,
            client=client,
        )
    return DecodeClient(settings.ASR_DECODE_URL, credential, client=client)
