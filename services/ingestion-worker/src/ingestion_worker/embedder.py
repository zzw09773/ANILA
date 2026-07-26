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
from anila_core.memory import EMBED_DIM, EMBED_NATIVE_DIM, truncate_embedding

from ingestion_worker.settings import WorkerSettings


logger = logging.getLogger(__name__)


def _sorted_by_index(items: list) -> list:
    """依 ``data[].index`` 排序，使回應對齊回請求 ``input`` 的原始位置。

    OpenAI 相容的批次 embeddings 端點不保證 ``data[]`` 陣列順序等於 ``input``
    順序（例如伺服端平行處理後亂序回傳），只保證每個 item 帶的 ``index`` 欄位
    指回原始位置。缺 ``index``（非標準/舊端點）時退回原陣列順序（``sorted`` 為
    穩定排序，此時每個 key 即原始位置本身，等同不動）。
    """
    return [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: (
                pair[1]["index"]
                if isinstance(pair[1], dict) and "index" in pair[1]
                else pair[0]
            ),
        )
    ]


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
        except httpx.RequestError as e:
            raise EmbedError(
                code="E_EMBED_MODEL_DOWN",
                retryable=True,
                severity="error",
                user_message="Embedding endpoint is temporarily unreachable.",
                details={"cause": type(e).__name__},
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
        # OpenAI-compatible response: { data: [{embedding: [...], index: int}, ...] }.
        # Sort by index before zipping so vectors stay positionally aligned with
        # `texts` even when the endpoint returns `data[]` out of request order.
        try:
            vectors = [item["embedding"] for item in _sorted_by_index(data["data"])]
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

        if self._settings.embedding_dim != EMBED_DIM:
            raise EmbedError(
                code="E_EMBED_DIM_MISMATCH",
                retryable=False,
                severity="error",
                user_message=(
                    f"Worker embedding_dim is {self._settings.embedding_dim}, but the "
                    f"shared storage contract requires {EMBED_DIM}."
                ),
                details={
                    "got": self._settings.embedding_dim,
                    "expected": EMBED_DIM,
                    "source": "worker_settings",
                },
            )

        # Use the platform's single embedding contract: an already-normalized
        # 4000-d vector, NV-Embed's native 4096-d vector, or a vector at the
        # explicitly declared ``embedding_source_dim`` (zero-padded).  In
        # particular, an arbitrary vector must never be silently sliced or
        # padded into a syntactically valid but semantically corrupt database
        # value — which is exactly what happens if short vectors are accepted
        # unconditionally and the endpoint later serves a different model.
        source_dim = self._settings.embedding_source_dim
        normalized_vectors: list[list[float]] = []
        for i, v in enumerate(vectors):
            try:
                normalized_vectors.append(truncate_embedding(v, pad_from=source_dim))
            except ValueError as exc:
                accepted = (
                    f"{EMBED_DIM}-d storage vectors or {EMBED_NATIVE_DIM}-d native vectors"
                )
                if source_dim is not None:
                    accepted += (
                        f", or the declared {source_dim}-d model output "
                        f"(zero-padded to {EMBED_DIM})"
                    )
                raise EmbedError(
                    code="E_EMBED_DIM_MISMATCH",
                    retryable=False,
                    severity="error",
                    user_message=(
                        f"Embedding {i} is {len(v)}-d; the shared contract accepts "
                        f"{accepted}. If a smaller-dimension model is deployed on "
                        "purpose, declare it via embedding_source_dim so that an "
                        "endpoint serving something else still fails loudly."
                    ),
                    details={
                        "got": len(v),
                        "expected": EMBED_DIM,
                        "native": EMBED_NATIVE_DIM,
                        "index": i,
                    },
                ) from exc
        vectors = normalized_vectors

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
