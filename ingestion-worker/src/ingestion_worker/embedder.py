"""Embedding endpoint client.

OpenAI-compatible POST to ``/v1/embeddings`` with batched ``input``. The
endpoint is configured via ``WorkerSettings.embedding_*``.

Sprint 5 / Chunk W: ``embedding_base_url`` now points at the CSP proxy
(``http://csp:8000/v1``) rather than the embedding endpoint directly.
CSP forwards to the real embedder AND writes a ``token_usage`` row
with ``request_type='embedding'`` per request, so usage tracking is
consolidated under one code path (proxy_service.proxy_request). The
worker's own ad-hoc usage-record path was removed — there's only one
metering point now.

Sprint 1 dim contract: schema is ``halfvec(4000)`` (migration 0015).
The deployed embedder returns native NV-embed-V2 4096-d and ignores
the OpenAI ``dimensions`` truncation param, so we truncate client-side
(drop the trailing 96 dims; well below the Matryoshka noise floor).

We assert the dim on every response — a wrong-dim INSERT into
``halfvec(4000)`` would only fail at the asyncpg layer with a less
helpful error.

Retry policy is intentionally NOT here. The worker's job-level retry
(via Arq) handles transient failures uniformly.
"""

from __future__ import annotations

import logging

import httpx

from anila_core.ingestion.errors import EmbedError

from ingestion_worker.settings import WorkerSettings


logger = logging.getLogger(__name__)


class Embedder:
    """One-shot embedding client. Stateless; cheap to construct per-job.

    Sprint 5 routing: ``settings.embedding_base_url`` points at the CSP
    proxy. CSP authenticates the worker via the ``embedding_api_key``
    Bearer token (auto-seeded as the ``ingestion-worker`` system user)
    and writes the ``token_usage`` row itself. The worker doesn't need
    a pool reference any more — usage tracking is centralised on the
    CSP side.
    """

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        # Build the client once per Embedder so connection pooling is
        # reused across the .embed() calls of a single job.
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        )

    async def embed(
        self,
        texts: list[str],
        *,
        user_id: int | None = None,
    ) -> list[list[float]]:
        """Return one vector per input text in the same order.

        Batches everything in a single request — most OpenAI-compatible
        endpoints accept up to ~8k tokens of input combined, which is
        comfortable for typical chunk batches (e.g. 50 chunks ×
        average 300 tokens each = 15k chars / ~3.7k tokens).
        """
        if not texts:
            return []
        try:
            r = await self._client.post(
                "/embeddings",
                json={
                    "model": self._settings.embedding_model,
                    "input": texts,
                },
            )
        except httpx.TimeoutException as e:
            raise EmbedError.timeout(
                user_message="Embedding endpoint timed out.",
                details={"timeout_s": self._settings.embedding_timeout_seconds},
            ) from e

        if r.status_code != 200:
            raise EmbedError(
                code="E_EMBED_MODEL_DOWN",
                retryable=True,
                severity="error",
                user_message=(
                    f"Embedding endpoint returned HTTP {r.status_code}; "
                    f"check service status."
                ),
                details={
                    "status_code": r.status_code,
                    "body_snippet": r.text[:500],
                },
            )

        data = r.json()
        # OpenAI-compatible response: { data: [{embedding: [...]}, ...] }
        try:
            vectors = [item["embedding"] for item in data["data"]]
        except (KeyError, TypeError) as e:  # noqa: F841 — used in raise from
            # Fall through to the explicit raise below.
            raise EmbedError(
                code="E_EMBED_MODEL_DOWN",
                retryable=False,
                severity="error",
                user_message=(
                    "Embedding endpoint returned an unexpected payload shape "
                    "(missing data[].embedding)."
                ),
                details={
                    "response_keys": list(data) if isinstance(data, dict) else "<not-dict>"
                },
            ) from e

        # Server-side truncation isn't supported (proxy ignores OpenAI's
        # ``dimensions`` parameter as of 2026-04-25), so we drop the
        # tail dims here. NV-embed-V2 native 4096 → schema 4000 = drop 96.
        # Truncation must happen *before* dim assert so the assertion
        # checks the post-truncation shape.
        target_dim = self._settings.embedding_dim
        truncated_vectors: list[list[float]] = []
        for v in vectors:
            if len(v) >= target_dim:
                truncated_vectors.append(v[:target_dim])
            else:
                # Endpoint returned fewer dims than the schema — that's
                # the model-mismatch error code, not a truncation case.
                truncated_vectors.append(v)
        vectors = truncated_vectors

        if len(vectors) != len(texts):
            raise EmbedError(
                code="E_EMBED_MODEL_DOWN",
                retryable=False,
                severity="error",
                user_message=(
                    f"Embedding endpoint returned {len(vectors)} vectors for "
                    f"{len(texts)} inputs; alignment broken."
                ),
                details={"input_count": len(texts), "output_count": len(vectors)},
            )

        # Dim contract — fail fast, don't let asyncpg complain mid-INSERT.
        expected = self._settings.embedding_dim
        for i, v in enumerate(vectors):
            if len(v) != expected:
                raise EmbedError(
                    code="E_EMBED_DIM_MISMATCH",
                    retryable=False,
                    severity="error",
                    user_message=(
                        f"Embedding {i} is {len(v)}-d but the schema requires "
                        f"{expected}-d. The collection was created against a "
                        f"different model — recreate the collection or change "
                        f"the embedding model env."
                    ),
                    details={"got": len(v), "expected": expected, "index": i},
                )

        # Sprint 5 / Chunk W: usage tracking happens on the CSP side
        # (proxy_service.proxy_request writes the token_usage row with
        # request_type='embedding'). The ``user_id`` arg is kept for
        # callsite compatibility — we don't need it here because CSP
        # attributes the call via the ``ingestion-worker`` system API
        # key. Future: pass user_id as ``X-Anila-Bill-To-User`` header
        # if we want to bill to the uploading user instead of the
        # worker's system user.
        del user_id  # explicitly discarded; see comment above
        return vectors

    async def close(self) -> None:
        await self._client.aclose()
