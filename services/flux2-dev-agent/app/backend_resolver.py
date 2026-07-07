"""Resolve which FLUX endpoint/model a generation request should use.

CSP's image-primary (via ``ImagePrimaryFetcher``) wins when it has a
usable value; otherwise falls back to the env-configured
``FLUX_BACKEND_URL`` / ``FLUX_MODEL``. Raises ``FluxBackendUnconfigured``
when neither source has a value, so the caller surfaces a clear error
instead of silently degrading (main.py's ``/v1/chat/completions``
handler already converts any handler exception into a 502).
"""
from __future__ import annotations

from typing import Optional, Protocol


class FluxBackendUnconfigured(RuntimeError):
    """Neither CSP image-primary nor env fallback provided a FLUX backend."""


class _ImagePrimaryFetcherProto(Protocol):
    async def get(self) -> tuple[Optional[str], Optional[str]]: ...


class BackendResolver:
    def __init__(
        self,
        *,
        fetcher: Optional[_ImagePrimaryFetcherProto],
        fallback_endpoint: str,
        fallback_model: str,
    ) -> None:
        self._fetcher = fetcher
        self._fallback_endpoint = fallback_endpoint
        self._fallback_model = fallback_model

    async def resolve(self) -> tuple[str, str]:
        if self._fetcher is not None:
            endpoint, model = await self._fetcher.get()
            if endpoint and model:
                return endpoint, model
        if self._fallback_endpoint and self._fallback_model:
            return self._fallback_endpoint, self._fallback_model
        raise FluxBackendUnconfigured(
            "沒有可用的 FLUX 端點/模型：CSP 未設定主圖像模型，"
            "且 env FLUX_BACKEND_URL/FLUX_MODEL 亦未設定"
        )
