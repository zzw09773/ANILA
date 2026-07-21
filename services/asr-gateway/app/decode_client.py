"""asr-decoder 的 HTTP 客戶端。

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

import httpx
import numpy as np

logger = logging.getLogger(__name__)

PARTIAL_TIMEOUT_SECONDS = 4.0
FINAL_TIMEOUT_SECONDS = 30.0

KIND_PARTIAL = "partial"
KIND_FINAL = "final"


class DecodeError(RuntimeError):
    """final 解碼失敗。partial 失敗不會走到這裡(靜默丟棄)。"""


class DecodeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.AsyncClient()

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
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        params = {
            "kind": kind,
            "beam": beam,
            "prompt": prompt or "",
            "language": language,
        }
        timeout = PARTIAL_TIMEOUT_SECONDS if kind == KIND_PARTIAL else FINAL_TIMEOUT_SECONDS
        try:
            resp = await self._client.post(
                f"{self._base_url}/transcribe",
                params=params,
                content=pcm,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Token": self._token,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if kind == KIND_PARTIAL:
                # 不記音訊內容,也不吵 —— partial 掉了是預期內的事。
                logger.debug("partial decode dropped: %s", exc)
                return {"text": ""}
            raise DecodeError(f"GPU 解碼無回應({exc})") from exc
