"""Vision-capable LLM provider (OpenAI-compatible multimodal).

Calls a VLM endpoint (e.g. maverick4, gemma4 with vision) using the
OpenAI ``messages`` format with ``content`` lists that mix text and
``image_url`` items. Images are passed as base64 data URIs — the same
shape used by vLLM, NIM, and the official OpenAI chat API.

Typical use during ingestion:

    vlm = VisionProvider(base_url=..., model=..., api_key=...)
    caption = await vlm.describe_image(image_bytes, mime="image/png")

The provider is intentionally minimal: ingestion only needs a single
``describe_image`` call per image. For tool-driven VLM reasoning, go
through ``providers/openai_compat.py`` directly.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


_DEFAULT_PROMPT = (
    "Describe this image in concise, factual terms so it can be indexed for "
    "search. Include any visible text verbatim (OCR), diagrams, tables, "
    "charts, or symbols. Do not add interpretation or opinion. "
    "Respond in the same language as any text that appears in the image; "
    "otherwise respond in Traditional Chinese."
)


class VisionProvider:
    """OpenAI-compatible vision provider.

    Args:
        base_url:    Base URL of the VLM endpoint (without /chat/completions suffix).
        api_key:     Bearer token; use 'not-set' for open local deployments.
        model:       Vision model identifier (e.g. meta/llama-4-maverick).
        timeout:     HTTP request timeout in seconds.
        verify_ssl:  Set False for self-signed internal certs.
        max_image_bytes: Reject images larger than this (prevents OOM on the VLM).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "not-set",
        model: str = "meta/llama-4-maverick",
        timeout: float = 120.0,
        verify_ssl: bool = False,
        max_image_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._max_image_bytes = max_image_bytes

    @property
    def model(self) -> str:
        return self._model

    async def describe_image(
        self,
        image_bytes: bytes,
        mime: str = "image/png",
        prompt: Optional[str] = None,
        max_tokens: int = 512,
    ) -> str:
        """Return a textual description of a single image.

        Returns an empty string when the image is empty, oversized, or the
        VLM call fails — ingestion must continue even when a single image
        cannot be described.
        """
        if not image_bytes:
            return ""
        if len(image_bytes) > self._max_image_bytes:
            logger.warning(
                "Image %d bytes exceeds max %d — skipping VLM description",
                len(image_bytes),
                self._max_image_bytes,
            )
            return ""

        data_uri = _to_data_uri(image_bytes, mime)
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or _DEFAULT_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(
                headers=self._headers,
                timeout=self._timeout,
                verify=self._verify_ssl,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("VLM describe_image failed: %s", exc)
            return ""

        try:
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = "".join(parts)
            return (content or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("VLM response has unexpected shape: %s", exc)
            return ""


def _to_data_uri(image_bytes: bytes, mime: str) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class MockVisionProvider:
    """Test double for VisionProvider — returns deterministic captions."""

    def __init__(self, caption: str = "[mock image caption]") -> None:
        self._caption = caption
        self.calls: list[tuple[int, str]] = []

    @property
    def model(self) -> str:
        return "mock-vision"

    async def describe_image(
        self,
        image_bytes: bytes,
        mime: str = "image/png",
        prompt: Optional[str] = None,
        max_tokens: int = 512,
    ) -> str:
        self.calls.append((len(image_bytes), mime))
        return self._caption
