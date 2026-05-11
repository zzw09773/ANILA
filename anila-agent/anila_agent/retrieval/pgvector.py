"""pgvector retriever via langchain_postgres. One-line config:

    PGVECTOR_URL=postgresql+psycopg2://user:pass@host:port/db
    PGVECTOR_COLLECTION=collection_name
    ANILA_EMBED_MODEL=text-embedding-3-small        # optional
    ANILA_EMBED_BASE_URL=https://...                # optional, falls back to ANILA_BASE_URL
    ANILA_EMBED_API_KEY=sk-...                      # optional, falls back to ANILA_API_KEY

`langchain-postgres` and `langchain-openai` are imported lazily so the rest
of the starter runs without them. If `PGVECTOR_URL` is set but the packages
are missing, `from_env()` raises with the install command.
"""

from __future__ import annotations

import os
from typing import Any

from anila_agent.models.schemas import Document


class PgVectorRetriever:
    """Thin wrapper over langchain_postgres.PGVector."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        embed_model: str = "text-embedding-3-small",
        embed_base_url: str | None = None,
        embed_api_key: str | None = None,
    ) -> None:
        try:
            from langchain_openai import OpenAIEmbeddings
            from langchain_postgres import PGVector
        except ImportError as e:
            raise ImportError(
                "PgVectorRetriever requires langchain-postgres + langchain-openai. "
                "Install with: uv pip install langchain-postgres langchain-openai psycopg[binary]"
            ) from e

        embeddings = OpenAIEmbeddings(
            model=embed_model,
            base_url=embed_base_url,
            api_key=embed_api_key,
        )
        self._store = PGVector(
            embeddings=embeddings,
            collection_name=collection,
            connection=url,
            use_jsonb=True,
        )
        self._collection = collection

    @property
    def name(self) -> str:
        return f"pgvector:{self._collection}"

    @property
    def metadata(self) -> dict[str, Any]:
        return {"backend": "pgvector", "collection": self._collection}

    async def search(self, query: str, k: int = 5) -> list[Document]:
        results = await self._store.asimilarity_search_with_score(query, k=k)
        out: list[Document] = []
        for i, (doc, score) in enumerate(results):
            doc_id = (
                getattr(doc, "id", None)
                or doc.metadata.get("id")
                or doc.metadata.get("chunk_id")
                or str(i)
            )
            out.append(
                Document(
                    id=str(doc_id),
                    text=doc.page_content,
                    score=float(score),
                    metadata=dict(doc.metadata),
                )
            )
        return out

    async def fetch(self, doc_id: str) -> Document | None:
        # Chunks are the unit of retrieval; the search hit already carries
        # the full content. Return None so the agent uses search results directly.
        return None


def from_env() -> PgVectorRetriever | None:
    """Build a PgVectorRetriever from environment variables.

    Returns None when `PGVECTOR_URL` is unset (caller stays on DummyRetriever).
    Raises when the URL is set but the collection name is missing — silent
    fallback would mask a deployment mistake.
    """
    url = os.environ.get("PGVECTOR_URL")
    if not url:
        return None
    collection = os.environ.get("PGVECTOR_COLLECTION")
    if not collection:
        raise ValueError(
            "PGVECTOR_URL is set but PGVECTOR_COLLECTION is missing. "
            "Set both or unset both."
        )
    return PgVectorRetriever(
        url=url,
        collection=collection,
        embed_model=os.environ.get("ANILA_EMBED_MODEL", "text-embedding-3-small"),
        embed_base_url=(
            os.environ.get("ANILA_EMBED_BASE_URL") or os.environ.get("ANILA_BASE_URL")
        ),
        embed_api_key=(
            os.environ.get("ANILA_EMBED_API_KEY") or os.environ.get("ANILA_API_KEY")
        ),
    )
