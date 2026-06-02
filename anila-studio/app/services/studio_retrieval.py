"""Studio retrieval helpers — chunk + image fetch via the csp HTTP API.

Extracted from ``api/studio.py`` (god-module split). csp owns the embedding
model and the pgvector indexes; these helpers just call its search endpoints
and project the hits into the dict shapes the prompt builder / renderer
expect. Sole caller is studio's ``_run_pipeline``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.clients.csp_client import get_collection, search_chunks, search_images
from app.services.studio_config import (
    STUDIO_CONTENT_LIMIT_CHARS,
    STUDIO_IMAGE_MIN_SCORE,
    STUDIO_IMAGE_TOP_K,
    STUDIO_MIN_SCORE,
    STUDIO_TOP_K,
)

if TYPE_CHECKING:
    from app.clients.csp_client import ChunkHit


async def retrieve_chunks(
    bearer: str,
    collection_id: int,
    seed_query: str,
) -> list[dict[str, Any]]:
    """Top-K relevant chunks for the seed_query, fetched via csp HTTP.

    csp owns the embedding model + pgvector index; we just call its
    ``/api/ingestion/collections/{id}/search`` endpoint and project the
    returned hits into the dict shape the prompt builder expects.

    Returns ``[]`` when:
      * the collection is archived (csp returns its meta but Studio
        treats archived as "no retrieval");
      * csp returns an empty result set;
      * csp surfaces ``CspNotFoundError`` (already-deleted collection).

    Raises:
      * ``CspForbiddenError`` (caller already 403'd in the POST handler;
        if we re-hit it here it's a TOCTOU race — surface as 403).
      * ``CspUnauthorizedError`` (token expired mid-job — surface as 401
        so the SPA refreshes and retries).
      * ``CspServerError`` (transient csp outage; caller catches this
        and degrades to "no retrieval" mode).
    """
    coll = await get_collection(collection_id, bearer=bearer)
    if coll.status != "active":
        # Studio over an archived collection is an unusual ask; treat as
        # zero hits and let the prompt fall through to "no context" mode.
        return []

    hits = await search_chunks(
        collection_id,
        seed_query,
        top_k=STUDIO_TOP_K,
        min_score=STUDIO_MIN_SCORE,
        bearer=bearer,
    )
    return _build_chunk_dicts(hits)


def _build_chunk_dicts(hits: list["ChunkHit"]) -> list[dict[str, Any]]:
    """Project ``ChunkHit`` dataclasses into the dict shape callers expect.

    The original csp implementation joined filenames out of
    ``ingestion_documents`` separately; the new csp HTTP endpoint
    embeds ``filename`` on every hit, so the join here is a no-op.
    Content is truncated client-side to ``STUDIO_CONTENT_LIMIT_CHARS``
    to keep the prompt budget bounded even if csp returned larger
    chunks than the studio target.
    """
    return [
        {
            "filename": h.filename or "<unknown>",
            "chunk_key": h.chunk_key,
            "content": h.content[:STUDIO_CONTENT_LIMIT_CHARS],
            "score": float(h.score),
        }
        for h in hits
    ]


async def retrieve_images(
    bearer: str,
    collection_id: int,
    seed_query: str,
) -> list[dict[str, Any]]:
    """Top-K relevant ingestion_images rows for the deck topic.

    Phase 5. Mirrors ``retrieve_chunks`` but calls csp's
    ``/api/ingestion/collections/{id}/images/search`` endpoint instead
    of the chunk-search route. Returns a list of dicts the prompt
    builder can splat into the "可用圖" section; the renderer-side
    hydration step (``_hydrate_images``) resolves ``image_id`` to PNG
    bytes via ``csp_client.fetch_image_blob``.

    Empty list when:
      * collection has no images at all (text-only knowledge base);
      * embedder returned an empty vector;
      * pgvector match scores are all below threshold;
      * collection is archived (csp returns meta but Studio treats
        archived as "no retrieval").
    """
    coll = await get_collection(collection_id, bearer=bearer)
    if coll.status != "active":
        return []

    hits = await search_images(
        collection_id,
        seed_query,
        top_k=STUDIO_IMAGE_TOP_K,
        min_score=STUDIO_IMAGE_MIN_SCORE,
        bearer=bearer,
    )

    return [
        {
            "image_id": h.image_id,
            "document_id": h.document_id,
            "page": h.page,
            "storage_path": h.storage_path,
            "mime": h.mime,
            "caption": (h.caption or "").strip(),
            "filename": h.filename,
            "score": float(h.score),
        }
        for h in hits
    ]
