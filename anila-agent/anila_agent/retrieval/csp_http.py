"""Retriever that fetches chunks via the CSP HTTP search API.

Unlike :mod:`anila_agent.retrieval.anila_pgvector` (direct Postgres) this goes
through CSP's authenticated ``POST /api/ingestion/collections/{id}/search``
endpoint. An agent therefore needs only a CSP API key — no Postgres
credentials and no network path to the database. CSP enforces collection
access (admin or owner), embeds the query with the collection's configured
model, and applies RLS, so the agent stays fully decoupled from storage and
embedding details. This is the recommended built-in retriever for agents that
already authenticate to CSP.

One-liner config:

    ANILA_CSP_BASE_URL=https://csp.internal      # CSP origin (NOT the /v1 proxy base)
    ANILA_COLLECTION_ID=<int collection id>
    ANILA_CSP_API_KEY=sk-...                      # falls back to ANILA_API_KEY
    ANILA_CSP_MIN_SCORE=0.25                      # optional; default 0.25
    ANILA_SSL_VERIFY=0                            # 1 by default; 0 for self-signed certs
"""

from __future__ import annotations

import os
from typing import Any

from anila_agent.models.schemas import Document

_DEFAULT_MIN_SCORE = 0.25


class CspHttpRetriever:
    """Top-K semantic search over a CSP collection via its HTTP search API.

    Stateless apart from a per-call ``httpx.AsyncClient``; cheap to construct.
    ``search()`` POSTs the query to CSP, which embeds + runs the pgvector
    similarity search server-side, then maps each hit into a :class:`Document`.
    """

    def __init__(
        self,
        *,
        csp_base_url: str,
        collection_id: int,
        api_key: str,
        min_score: float = _DEFAULT_MIN_SCORE,
        verify_ssl: bool = True,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(collection_id, int) or isinstance(collection_id, bool):
            raise ValueError(
                f"collection_id must be int, got {type(collection_id).__name__}"
            )
        if collection_id <= 0:
            raise ValueError(f"collection_id must be > 0, got {collection_id}")
        if not csp_base_url:
            raise ValueError("csp_base_url must be a non-empty CSP origin")
        if not api_key:
            raise ValueError("api_key must be a non-empty CSP API key")
        self._base_url = csp_base_url.rstrip("/")
        self._collection_id = collection_id
        self._api_key = api_key
        self._min_score = min_score
        self._verify_ssl = verify_ssl
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"csp-http:collection={self._collection_id}"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "backend": "csp-http",
            "collection_id": self._collection_id,
            "base_url": self._base_url,
        }

    @property
    def _search_url(self) -> str:
        return (
            f"{self._base_url}/api/ingestion/collections/"
            f"{self._collection_id}/search"
        )

    async def search(self, query: str, k: int = 5) -> list[Document]:
        import httpx

        async with httpx.AsyncClient(
            verify=self._verify_ssl, timeout=self._timeout
        ) as client:
            response = await client.post(
                self._search_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "query": query,
                    "top_k": k,
                    "min_score": self._min_score,
                },
            )
            response.raise_for_status()
            payload = response.json()

        results = payload.get("results") or []
        documents: list[Document] = []
        for hit in results:
            if not isinstance(hit, dict):
                continue
            documents.append(
                Document(
                    id=str(hit.get("chunk_id")),
                    text=hit.get("content", "") or "",
                    score=float(hit.get("score", 0.0) or 0.0),
                    metadata={
                        "chunk_key": hit.get("chunk_key"),
                        "document_id": hit.get("document_id"),
                        "filename": hit.get("filename"),
                        **(hit.get("metadata") or {}),
                    },
                )
            )
        return documents

    async def fetch(self, doc_id: str) -> Document | None:
        # Chunks already carry full content from search; no separate fetch path
        # (mirrors AnilaPgVectorRetriever).
        return None


def from_env() -> CspHttpRetriever | None:
    """Build from env. Returns None when not configured.

    Activation requires BOTH ``ANILA_CSP_BASE_URL`` and ``ANILA_COLLECTION_ID``
    — the base-url gate keeps this distinct from the direct-pgvector retriever
    (which activates on ``ANILA_COLLECTION_ID`` + ``PGVECTOR_URL``). The API key
    falls back to ``ANILA_API_KEY`` when ``ANILA_CSP_API_KEY`` is unset (common
    when chat + retrieval share one key).
    """
    base = os.environ.get("ANILA_CSP_BASE_URL")
    cid_raw = os.environ.get("ANILA_COLLECTION_ID")
    if not base or not cid_raw:
        return None
    try:
        cid = int(cid_raw)
    except ValueError as e:
        raise ValueError(
            f"ANILA_COLLECTION_ID must be an int, got {cid_raw!r}"
        ) from e

    api_key = os.environ.get("ANILA_CSP_API_KEY") or os.environ.get("ANILA_API_KEY")
    if not api_key:
        raise ValueError(
            "ANILA_CSP_BASE_URL is set but no API key found "
            "(set ANILA_CSP_API_KEY or ANILA_API_KEY)."
        )

    try:
        min_score = float(os.environ.get("ANILA_CSP_MIN_SCORE", _DEFAULT_MIN_SCORE))
    except ValueError as e:
        raise ValueError("ANILA_CSP_MIN_SCORE must be a float") from e

    verify_raw = os.environ.get("ANILA_SSL_VERIFY", "1").lower()
    verify_ssl = verify_raw not in ("0", "false", "no", "off")

    return CspHttpRetriever(
        csp_base_url=base,
        collection_id=cid,
        api_key=api_key,
        min_score=min_score,
        verify_ssl=verify_ssl,
    )
