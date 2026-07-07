"""HTTP client for the FLUX image generation backend(OpenAI 相容)。

院內模型統一部署在雲端算力中心後,打圖改走標準 OpenAI Images API:

    POST {base}/v1/images/generations
    request : {model, prompt, n, size, response_format:"b64_json"}
              (+ 可選 Authorization: Bearer)
    response: {created, data:[{b64_json}]}

base URL 有無 ``/v1`` 結尾皆可(比照 csp memory_service._embed 的
正規化:沒帶版本段就補 /v1)。這個 shim 只需要一張圖(n=1),解出
第一個 ``b64_json`` 還原成 PNG bytes。seed / num_inference_steps /
guidance_scale 不是 OpenAI 標準欄位,不上 wire。
"""
from __future__ import annotations

import base64
import binascii
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# aspect_ratio → OpenAI ``size`` 對映表。1:1 / 16:9 / 9:16 用 OpenAI
# (dall-e-3)標準尺寸;4:3 / 3:4 / 3:1 沿用 flux2-dev 原生解析度
# (FLUX ≤0.8MP 穩定區,相容伺服器普遍接受任意 WxH)。
# 未知 aspect → _DEFAULT_SIZE(fallback 預設)。
_ASPECT_TO_SIZE: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "4:3": "1216x896",
    "3:4": "896x1216",
    "3:1": "1536x512",
}
_DEFAULT_SIZE = "1024x1024"


class FluxBackendError(RuntimeError):
    pass


class FluxClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 120.0,
        *,
        model: str = "flux.2-dev",
        api_key: str = "",
    ) -> None:
        base = base_url.rstrip("/")
        # /v1 正規化:伺服器根或含 /v1 的 base 皆可。
        if not base.endswith(("/v1", "/v2")):
            base = f"{base}/v1"
        self._base_url = base
        self._timeout = timeout
        self._model = model
        self._api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "FluxClient":
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._client = httpx.AsyncClient(timeout=self._timeout, headers=headers)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, prompt: str, aspect_ratio: str) -> bytes:
        if self._client is None:
            raise RuntimeError("FluxClient must be used as an async context manager")

        size = _ASPECT_TO_SIZE.get(aspect_ratio)
        if size is None:
            logger.debug(
                "unknown aspect_ratio %r; falling back to size %s",
                aspect_ratio, _DEFAULT_SIZE,
            )
            size = _DEFAULT_SIZE

        resp = await self._client.post(
            f"{self._base_url}/images/generations",
            json={
                "model": self._model,
                "prompt": prompt,
                "n": 1,
                "size": size,
                "response_format": "b64_json",
            },
        )
        if resp.status_code != 200:
            raise FluxBackendError(
                f"flux backend returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise FluxBackendError(
                f"flux backend returned non-JSON body: {resp.text[:200]}"
            ) from exc
        items = data.get("data")
        if not items:
            raise FluxBackendError(
                f"flux backend returned no images: {str(data)[:200]}"
            )
        try:
            # validate=True：b64decode 預設會靜默丟棄非法字元（"!!!!" → b""），
            # 必須顯式驗證才會 raise，否則壞回應變成空圖往下游流。
            png = base64.b64decode(items[0]["b64_json"], validate=True)
        except (KeyError, TypeError, binascii.Error, ValueError) as exc:
            raise FluxBackendError(
                f"flux backend returned malformed b64_json: {str(data)[:200]}"
            ) from exc
        if not png:
            raise FluxBackendError(
                f"flux backend returned empty image payload: {str(data)[:200]}"
            )
        return png
