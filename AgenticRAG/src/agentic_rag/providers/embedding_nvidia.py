"""NV-Embed-V2 Embedding Provider via OpenAI-compatible embeddings endpoint.

Compatible with:
  - Nvidia NIM API (https://integrate.api.nvidia.com/v1/embeddings)
  - Self-hosted TEI / Triton Inference Server with OpenAI-compat wrapper

Model specs:
  - Model: nvidia/NV-Embed-v2  (or Nvidia/NV-embed-V2)
  - Vector dimension: 4096
  - Max input tokens: 32768
  - Batch limit: 50 inputs per request (NIM constraint)
  - Supports input_type: "passage" (for indexing) or "query" (for retrieval)
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # NIM API hard limit


class NvidiaEmbeddingProvider:
    """EmbeddingProvider backed by Nvidia NV-Embed-V2.

    Args:
        base_url:    Base URL of the embeddings endpoint (without /embeddings suffix).
        api_key:     Bearer token for the API (use 'not-set' for local inference).
        model:       Model identifier (default: Nvidia/NV-embed-V2).
        timeout:     HTTP request timeout in seconds.
        verify_ssl:  Set False when using self-signed certs on internal hosts.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "not-set",
        model: str = "Nvidia/NV-embed-V2",
        timeout: float = 60.0,
        verify_ssl: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self._verify_ssl = verify_ssl

    # ------------------------------------------------------------------
    # EmbeddingProvider Protocol
    # ------------------------------------------------------------------

    async def embed(
        self,
        texts: list[str],
        input_type: str = "passage",
    ) -> list[list[float]]:
        """Return 4096-dim embedding vectors for each input text.

        Args:
            texts:      List of strings to embed.
            input_type: "passage" for document indexing, "query" for retrieval.
        """
        if not texts:
            return []

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i : i + _BATCH_SIZE]
            batch_embeddings = await self._call_api(batch, input_type)
            all_embeddings.extend(batch_embeddings)

        return all_embeddings

    @property
    def dimension(self) -> int:
        """NV-Embed-V2 vector dimension."""
        return 4096

    # ------------------------------------------------------------------
    # Internal HTTP call
    # ------------------------------------------------------------------

    async def _call_api(
        self, texts: list[str], input_type: str
    ) -> list[list[float]]:
        payload: dict = {
            "model": self._model,
            "input": texts,
            "input_type": input_type,
            "encoding_format": "float",
        }

        async with httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
            verify=self._verify_ssl,
        ) as client:
            response = await client.post(
                f"{self._base_url}/embeddings",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        # Sort by index to ensure correct order
        items: list[dict] = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]
