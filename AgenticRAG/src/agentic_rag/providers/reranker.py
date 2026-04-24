"""Cross-encoder reranker for hybrid-search results.

Single backend: ``VllmScoreRerankerProvider`` — calls a vLLM-hosted
cross-encoder (deployment target: ``mixedbread-ai/mxbai-rerank-large-v1``,
served by vLLM with ``--task score``) over the OpenAI-compatible
``POST /v1/score`` endpoint.

The deployment is assumed to live on the same internal model server as
the LLM / embedding / vision endpoints, so there is no cloud-API or
local-CPU fallback path here. If you need either of those, the previous
designs are archived in ``_archive/phase4_pre_rewrite/``.
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


class VllmScoreRerankerProvider:
    """vLLM-hosted cross-encoder reranker via OpenAI-compatible ``/v1/score``.

    Request shape::

        POST {base_url}/score
        {
          "model":  "<model name>",
          "text_1": "<query>",
          "text_2": ["doc1", "doc2", ...]
        }

    Response shape::

        {
          "data": [
            {"index": 0, "score": 0.91},
            {"index": 1, "score": 0.45},
            ...
          ],
          ...
        }

    The endpoint returns one score per ``text_2`` document; this class
    sorts by score and truncates to ``top_k`` so the vLLM server doesn't
    need any reranking knowledge beyond pairwise scoring.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
        verify_ssl: bool = True,
    ) -> None:
        if not base_url:
            raise ValueError("VllmScoreRerankerProvider requires a non-empty base_url")
        if not model:
            raise ValueError("VllmScoreRerankerProvider requires a non-empty model")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
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
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        async with httpx.AsyncClient(
            verify=self._verify_ssl, timeout=self._timeout
        ) as client:
            resp = await client.post(
                f"{self._base_url}/score",
                headers=headers,
                json={
                    "model": self._model,
                    "text_1": query,
                    "text_2": documents,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        scored: list[tuple[RerankCandidate, float]] = []
        for item in data.get("data", []):
            try:
                idx = int(item.get("index", -1))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(candidates):
                continue
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            scored.append((candidates[idx], score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        scored = scored[:top_k]
        return [
            RerankedResult(candidate=cand, score=score, rank=rank)
            for rank, (cand, score) in enumerate(scored)
        ]


def build_reranker_from_env() -> Optional[Reranker]:
    """Construct a reranker from env, or ``None`` if disabled / misconfigured.

    Env:
      RAG_RERANKER_ENABLED     = "true" | "false"  (default: false)
      RAG_RERANKER_URL         = base URL of the vLLM OpenAI-compatible server,
                                 e.g. http://172.16.120.35:8001/v1
      RAG_RERANKER_MODEL       = served model name, e.g. mxbai-rerank-large-v1
      RAG_RERANKER_API_KEY     = optional bearer token
      RAG_RERANKER_VERIFY_SSL  = "true" | "false"  (default: true)
    """
    enabled = os.getenv("RAG_RERANKER_ENABLED", "false").lower() == "true"
    if not enabled:
        return None

    base_url = os.getenv("RAG_RERANKER_URL", "").strip()
    model = os.getenv("RAG_RERANKER_MODEL", "").strip()
    if not base_url or not model:
        logger.warning(
            "RAG_RERANKER_ENABLED=true but RAG_RERANKER_URL or "
            "RAG_RERANKER_MODEL is missing — reranker disabled"
        )
        return None

    return VllmScoreRerankerProvider(
        base_url=base_url,
        model=model,
        api_key=os.getenv("RAG_RERANKER_API_KEY", "").strip(),
        verify_ssl=os.getenv("RAG_RERANKER_VERIFY_SSL", "true").lower() == "true",
    )
