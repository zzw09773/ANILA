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

P4.8 dim contract: schema is ``halfvec(4000)`` (HNSW ceiling). The
designated platform model's native width is measured at designation
time; ``truncate_embedding(..., pad_from=)`` pads or truncates to the
column width. Vectors shorter than the column are rejected unless
``pad_from`` equals their length — unconditional padding was tried and
reverted (silent corruption when the endpoint drifts).
"""

from __future__ import annotations

import logging

import httpx

from anila_core.ingestion.errors import EmbedError
from anila_core.memory.long_term import EMBED_DIM, truncate_embedding

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

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        model_name: str | None = None,
        native_dim: int | None = None,
    ) -> None:
        self._settings = settings
        self._model_name = model_name or settings.embedding_model
        self._native_dim = native_dim
        # Build the client once per Embedder so connection pooling is
        # reused across the .embed() calls of a single job.
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_base_url,
            timeout=settings.embedding_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def native_dim(self) -> int:
        if isinstance(self._native_dim, int) and self._native_dim > 0:
            return self._native_dim
        return self._settings.embedding_dim

    @property
    def pad_from(self) -> int | None:
        n = self.native_dim
        if n == EMBED_DIM:
            return None
        return n

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
                    "model": self._model_name,
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

        # Normalise to the configured schema width. Production pins
        # embedding_dim=EMBED_DIM (4000) and goes through the shared
        # truncate_embedding contract (pad_from = measured native).
        # Tests occasionally use a smaller embedding_dim; keep the
        # historical slice/assert path for those so unit fixtures stay
        # cheap without forking the production contract.
        target_dim = self._settings.embedding_dim
        pad_from = self.pad_from
        normalized: list[list[float]] = []
        for i, v in enumerate(vectors):
            if target_dim == EMBED_DIM:
                try:
                    normalized.append(truncate_embedding(v, pad_from=pad_from))
                except ValueError as e:
                    raise EmbedError(
                        code="E_EMBED_DIM_MISMATCH",
                        retryable=False,
                        severity="error",
                        user_message=(
                            f"Embedding {i} is {len(v)}-d but the schema requires "
                            f"{target_dim}-d"
                            + (
                                f" (declared native pad_from={pad_from})"
                                if pad_from is not None
                                else ""
                            )
                            + ". The collection was created against a "
                            "different model — recreate the collection or change "
                            "the embedding model env."
                        ),
                        details={
                            "got": len(v),
                            "expected": target_dim,
                            "pad_from": pad_from,
                            "index": i,
                        },
                    ) from e
            else:
                if len(v) >= target_dim:
                    normalized.append(v[:target_dim])
                else:
                    normalized.append(v)
        vectors = normalized

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
