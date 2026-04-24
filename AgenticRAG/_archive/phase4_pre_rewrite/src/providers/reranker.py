"""Cross-encoder reranker for hybrid-search results.

Two backends, both non-China origin:

- **JinaRerankerProvider**: hosted ``jina-reranker-v2-base-multilingual``
  (Jina AI, Berlin). Zero local deps; needs ``JINA_API_KEY``.
- **LocalHFRerankerProvider**: local HuggingFace cross-encoder, defaults
  to ``mixedbread-ai/mxbai-rerank-base-v1`` (Mixedbread, Berlin). CPU
  works; slow. Pulls in transformers + torch on first call.

The API is intentionally tiny so callers can swap implementations via
``build_reranker_from_env()``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankCandidate:
    """One candidate fed into the reranker."""

    chunk_id: str
    content: str
    metadata: dict
    original_score: Optional[float] = None


@dataclass(frozen=True)
class RerankedResult:
    """One reranker output, sorted descending by score."""

    candidate: RerankCandidate
    score: float
    rank: int


@runtime_checkable
class Reranker(Protocol):
    async def rerank(
        self,
        query: str,
        candidates: list[RerankCandidate],
        top_k: int,
    ) -> list[RerankedResult]: ...


class JinaRerankerProvider:
    """Hosted Jina rerank endpoint.

    Default model ``jina-reranker-v2-base-multilingual`` handles
    Traditional Chinese well (Jina v2 trained on 100+ langs).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "jina-reranker-v2-base-multilingual",
        base_url: str = "https://api.jina.ai/v1",
        timeout: float = 30.0,
        verify_ssl: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("JinaRerankerProvider requires a non-empty api_key")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._verify_ssl = verify_ssl

    async def rerank(
        self,
        query: str,
        candidates: list[RerankCandidate],
        top_k: int,
    ) -> list[RerankedResult]:
        if not candidates or top_k <= 0:
            return []

        documents = [c.content for c in candidates]
        async with httpx.AsyncClient(
            verify=self._verify_ssl, timeout=self._timeout
        ) as client:
            resp = await client.post(
                f"{self._base_url}/rerank",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(top_k, len(documents)),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results: list[RerankedResult] = []
        for rank, item in enumerate(data.get("results", [])):
            idx = int(item.get("index", -1))
            if idx < 0 or idx >= len(candidates):
                continue
            score = float(item.get("relevance_score", 0.0))
            results.append(
                RerankedResult(candidate=candidates[idx], score=score, rank=rank)
            )
        return results


class LocalHFRerankerProvider:
    """Local HuggingFace cross-encoder reranker.

    Default model ``mixedbread-ai/mxbai-rerank-base-v1`` (Apache-2,
    Berlin). Lazy-loads on first ``rerank()`` call so import is cheap.
    """

    def __init__(
        self,
        model_name: str = "mixedbread-ai/mxbai-rerank-base-v1",
        max_length: int = 512,
    ) -> None:
        self._model_name = model_name
        self._max_length = max_length
        self._model = None
        self._tokenizer = None
        self._torch = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch  # type: ignore[import]
            from transformers import (  # type: ignore[import]
                AutoModelForSequenceClassification,
                AutoTokenizer,
            )
        except ImportError as exc:
            raise ImportError(
                "LocalHFRerankerProvider needs torch + transformers. "
                "Install with: pip install 'agentic-rag[rerank-local]'"
            ) from exc

        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self._model_name
        )
        self._model.eval()
        self._torch = torch
        logger.info("Loaded local reranker: %s", self._model_name)

    async def rerank(
        self,
        query: str,
        candidates: list[RerankCandidate],
        top_k: int,
    ) -> list[RerankedResult]:
        if not candidates or top_k <= 0:
            return []
        self._ensure_loaded()
        assert self._tokenizer is not None and self._model is not None
        assert self._torch is not None

        pairs = [(query, c.content) for c in candidates]
        inputs = self._tokenizer(
            pairs,
            padding=True,
            truncation=True,
            return_tensors="pt",
            max_length=self._max_length,
        )
        with self._torch.no_grad():
            logits = self._model(**inputs).logits
        scores = logits.squeeze(-1).tolist()
        if not isinstance(scores, list):
            scores = [scores]

        scored = sorted(
            zip(candidates, scores),
            key=lambda x: float(x[1]),
            reverse=True,
        )[:top_k]
        return [
            RerankedResult(candidate=c, score=float(s), rank=rank)
            for rank, (c, s) in enumerate(scored)
        ]


def build_reranker_from_env() -> Optional[Reranker]:
    """Construct a reranker from env, or ``None`` if disabled / misconfigured.

    Env:
      RAG_RERANKER_ENABLED  = "true" | "false"        (default: false)
      RAG_RERANKER_BACKEND  = "jina" | "local"        (default: jina)
      RAG_RERANKER_MODEL    = override default model
      JINA_API_KEY          = required when backend=jina
      JINA_BASE_URL         = override Jina endpoint
      JINA_VERIFY_SSL       = "true" | "false"        (default: true)
    """
    enabled = os.getenv("RAG_RERANKER_ENABLED", "false").lower() == "true"
    if not enabled:
        return None

    backend = os.getenv("RAG_RERANKER_BACKEND", "jina").lower()
    if backend == "jina":
        api_key = os.getenv("JINA_API_KEY", "").strip()
        if not api_key:
            logger.warning(
                "RAG_RERANKER_ENABLED=true but JINA_API_KEY missing — disabled"
            )
            return None
        return JinaRerankerProvider(
            api_key=api_key,
            model=os.getenv(
                "RAG_RERANKER_MODEL", "jina-reranker-v2-base-multilingual"
            ),
            base_url=os.getenv("JINA_BASE_URL", "https://api.jina.ai/v1"),
            verify_ssl=os.getenv("JINA_VERIFY_SSL", "true").lower() == "true",
        )

    if backend == "local":
        return LocalHFRerankerProvider(
            model_name=os.getenv(
                "RAG_RERANKER_MODEL", "mixedbread-ai/mxbai-rerank-base-v1"
            ),
        )

    logger.warning("Unknown RAG_RERANKER_BACKEND=%s — disabled", backend)
    return None
