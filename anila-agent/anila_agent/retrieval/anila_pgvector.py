"""Retriever for the ANILA platform's native pgvector schema.

Talks directly to `ingestion_collections` + `document_chunks` (halfvec column,
HNSW index, RLS via `anila.collection_id` GUC). Use this — instead of the
langchain_postgres-based `pgvector.py` — when retrieving from a collection
that was ingested through the ANILA platform's ingestion worker.

One-liner config:

    PGVECTOR_URL=postgresql://csp:csp@127.0.0.1:5433/csp
    ANILA_COLLECTION_ID=52
    ANILA_EMBED_BASE_URL=https://172.16.120.35/v1   # optional, falls back to ANILA_BASE_URL
    ANILA_EMBED_API_KEY=sk-...                       # optional, falls back to ANILA_API_KEY
    ANILA_EMBED_MODEL=nvidia/NV-embed-V2
    ANILA_SSL_VERIFY=0                               # for self-signed certs

Embedding dimension is auto-detected from `ingestion_collections.embedding_dim`
on the first call; queries are truncated/padded to that width before search,
matching the halfvec column shape.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from anila_agent.models.schemas import Document


def _parse_metadata(value: Any) -> dict[str, Any]:
    """asyncpg returns JSONB as a str by default — decode to dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _normalize_dsn(url: str) -> str:
    """asyncpg uses `postgresql://`; strip SQLAlchemy `+psycopg` / `+psycopg2` flavours."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _format_halfvec(values: list[float]) -> str:
    """Render a Python list as the pgvector text input `[v1,v2,...]`.

    halfvec accepts the same textual representation as vector. We pass as text
    + explicit `::halfvec` cast in the SQL so we don't need a registered codec
    (pgvector-python's asyncpg helpers register `vector` only, not `halfvec`).
    """
    return "[" + ",".join(format(v, ".6g") for v in values) + "]"


class AnilaPgVectorRetriever:
    """Cosine-similarity search over the ANILA platform's `document_chunks`.

    Each `search()` call:
      1. POSTs the query to `/v1/embeddings` on the configured endpoint.
      2. Truncates / pads the result to the collection's `embedding_dim`.
      3. Acquires a pool connection, opens a transaction, sets
         `SET LOCAL anila.collection_id = <id>` so RLS scopes the next query
         to this collection only, then runs HNSW similarity search.

    The pool + dimension are cached after first use; subsequent calls reuse them.
    """

    def __init__(
        self,
        *,
        url: str,
        collection_id: int,
        embed_base_url: str,
        embed_api_key: str,
        embed_model: str,
        verify_ssl: bool = True,
    ) -> None:
        if not isinstance(collection_id, int) or isinstance(collection_id, bool):
            raise ValueError(f"collection_id must be int, got {type(collection_id).__name__}")
        if collection_id <= 0:
            raise ValueError(f"collection_id must be > 0, got {collection_id}")
        self._dsn = _normalize_dsn(url)
        self._collection_id = collection_id
        self._embed_base_url = embed_base_url.rstrip("/")
        self._embed_api_key = embed_api_key
        self._embed_model = embed_model
        self._verify_ssl = verify_ssl
        self._pool: Any = None
        self._embed_dim: int | None = None

    @property
    def name(self) -> str:
        return f"anila-pgvector:collection={self._collection_id}"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "backend": "anila-pgvector",
            "collection_id": self._collection_id,
            "embed_model": self._embed_model,
        }

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        return self._pool

    async def _ensure_dim(self) -> int:
        if self._embed_dim is not None:
            return self._embed_dim
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT embedding_dim FROM ingestion_collections WHERE id = $1",
                self._collection_id,
            )
        if row is None:
            raise ValueError(
                f"collection_id={self._collection_id} not found in ingestion_collections"
            )
        self._embed_dim = int(row["embedding_dim"])
        return self._embed_dim

    async def _embed(self, text: str) -> list[float]:
        import httpx

        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=30.0) as client:
            response = await client.post(
                f"{self._embed_base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._embed_api_key}"},
                json={"model": self._embed_model, "input": text},
            )
            response.raise_for_status()
            return response.json()["data"][0]["embedding"]

    async def search(self, query: str, k: int = 5) -> list[Document]:
        full = await self._embed(query)
        dim = await self._ensure_dim()
        emb = full[:dim] if len(full) >= dim else full + [0.0] * (dim - len(full))
        emb_text = _format_halfvec(emb)

        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            # f-string is safe: __init__ enforces positive int.
            await conn.execute(
                f"SET LOCAL anila.collection_id = {self._collection_id}"
            )
            rows = await conn.fetch(
                """
                    SELECT id, document_id, chunk_key, content, metadata,
                           1 - (embedding <=> $1::halfvec) AS score
                      FROM document_chunks
                     WHERE chunk_type = 'leaf'
                     ORDER BY embedding <=> $1::halfvec
                     LIMIT $2
                    """,
                emb_text,
                k,
            )
        return [
            Document(
                id=str(row["id"]),
                text=row["content"],
                score=float(row["score"]),
                metadata={
                    **_parse_metadata(row["metadata"]),
                    "chunk_key": row["chunk_key"],
                    "document_id": row["document_id"],
                },
            )
            for row in rows
        ]

    async def fetch(self, doc_id: str) -> Document | None:
        # Chunks already carry full content from search; no separate fetch path.
        return None

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def from_env() -> AnilaPgVectorRetriever | None:
    """Build from env. Returns None when not configured.

    Required: PGVECTOR_URL + ANILA_COLLECTION_ID.
    Embedding endpoint falls back to ANILA_BASE_URL / ANILA_API_KEY when the
    embed-specific vars are unset (typical when chat + embed share an endpoint).
    """
    # ANILA_COLLECTION_ID is the activation signal — without it, this retriever
    # opts out so the caller can fall through to the langchain_postgres flavour
    # (or DummyRetriever) without any noise.
    cid_raw = os.environ.get("ANILA_COLLECTION_ID")
    if not cid_raw:
        return None
    try:
        cid = int(cid_raw)
    except ValueError as e:
        raise ValueError(
            f"ANILA_COLLECTION_ID must be an int, got {cid_raw!r}"
        ) from e

    url = os.environ.get("PGVECTOR_URL")
    if not url:
        raise ValueError(
            "ANILA_COLLECTION_ID is set but PGVECTOR_URL is missing. "
            "Set both or unset both."
        )

    embed_base = (
        os.environ.get("ANILA_EMBED_BASE_URL") or os.environ.get("ANILA_BASE_URL")
    )
    embed_key = (
        os.environ.get("ANILA_EMBED_API_KEY") or os.environ.get("ANILA_API_KEY")
    )
    embed_model = os.environ.get("ANILA_EMBED_MODEL", "nvidia/NV-embed-V2")
    if not embed_base:
        raise ValueError(
            "ANILA_EMBED_BASE_URL (or ANILA_BASE_URL) must be set when ANILA_COLLECTION_ID is set"
        )
    if not embed_key:
        raise ValueError(
            "ANILA_EMBED_API_KEY (or ANILA_API_KEY) must be set when ANILA_COLLECTION_ID is set"
        )

    verify_raw = os.environ.get("ANILA_SSL_VERIFY", "1").lower()
    verify_ssl = verify_raw not in ("0", "false", "no", "off")

    return AnilaPgVectorRetriever(
        url=url,
        collection_id=cid,
        embed_base_url=embed_base,
        embed_api_key=embed_key,
        embed_model=embed_model,
        verify_ssl=verify_ssl,
    )
