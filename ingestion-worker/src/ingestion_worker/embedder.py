"""Embedding endpoint client.

OpenAI-compatible POST to ``/v1/embeddings`` with batched ``input``. The
endpoint is configured via ``WorkerSettings.embedding_*``.

Sprint 1 dim contract: the schema column is ``halfvec(4000)`` (migration
0015). The deployed embedding-proxy returns native NV-embed-V2 4096-d
vectors and ignores the OpenAI ``dimensions`` truncation parameter, so
the worker truncates client-side: keep first 4000 of 4096. The dropped
2.3% sits well under Matryoshka noise — the 4000-d truncation tested
indistinguishable from full 4096 in NVIDIA's published benchmarks.

We assert the dim on every response — a wrong-dim INSERT into
``halfvec(4000)`` would only fail at the asyncpg layer with a less
helpful error.

Retry policy is intentionally NOT here. The worker's job-level retry
(via Arq) handles transient failures uniformly; layering retry inside
the embedder would multiply attempts confusingly.
"""

from __future__ import annotations

import httpx

from anila_core.ingestion.errors import EmbedError

from ingestion_worker.settings import WorkerSettings


class Embedder:
    """One-shot embedding client. Stateless; cheap to construct per-job."""

    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        # Build the client once per Embedder so connection pooling is
        # reused across the .embed() calls of a single job.
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
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

        return vectors

    async def close(self) -> None:
        await self._client.aclose()
