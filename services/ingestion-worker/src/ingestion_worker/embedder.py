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

Batching (2026-08-07): ``embed()`` splits its input into consecutive
slices of ``settings.embedding_batch_size`` and posts one request per
slice. Before this, a document's entire chunk list went out as a single
``input`` array, which cannot be made to work on the Triton gRPC path:
CSP embeds one text per ModelInfer and bounds the **whole call** at
``_wait_ready 5s + EMBEDDING_TIMEOUT`` (35 s at defaults), a budget that
does not grow with ``len(input)``. Past some document size every upload
failed as a whole, and the runbook's advice ("縮小批次") named something
no operator could actually do. See ``WorkerSettings.embedding_batch_size``
for the sizing rule.

Batches are issued **sequentially**. Concurrency here would multiply the
pressure on CSP's shared ``asyncio.to_thread`` executor — the resource
whose exhaustion the 35 s budget exists to prevent — so it is not a free
speed-up and is deliberately not done.

Partial-failure contract: batch *k* failing aborts the call. Batches
after *k* are never requested (tokens are not spent past a known
failure), nothing partial is returned, and the raised ``EmbedError``
keeps the original ``code``/``retryable``/``severity`` while naming the
range that failed in ``details`` (``batch_index``, ``batch_count``,
``failed_range``, ``embedded_before_failure``). It does **not** record
progress: the caller indexes chunks in one ``index_chunks`` call after
the full vector list exists (handlers.py), so there is no partial-index
state to resume into, and arq's ``max_tries=3`` would re-embed anything
we did remember — i.e. a resume feature without a schema change would
double-bill rather than save work.

What batching costs, stated in full (this platform meters usage, so it
is not a footnote):

- **Success path: identical bill.** CSP meters a request as
  ``sum(max(1, len(t.split())) for t in texts)`` (proxy/service.py:267),
  which is additive over texts, and the batches are an exact partition —
  so the total is what it was unbatched. Measured: 20 texts cost 60
  tokens at batch size 4 and at batch size 1000.
- **A failed attempt now costs money where it used to cost zero.** CSP
  calls ``enqueue_usage`` only after a successful call
  (proxy/service.py:291), so before batching a failed document billed
  **0** — one request, one failure, no usage row. Now batches 1..k-1
  each succeeded and each wrote a row, and arq re-runs the whole job up
  to ``max_tries=3``, re-billing them every time. Measured on a 20-text
  document whose clean pass is 60 tokens: mid-document permanent failure
  72 (was 0); last-batch permanent failure 144 (was 0); mid-document
  failure healing on try 3, 108 = 1.80x; last-batch failure healing on
  try 3, 156 = 2.60x. Bounded strictly under 3x a clean pass
  (``max_tries=3``, and the failing batch itself is never billed).
  Shrinking the batch size does **not** lower that bound; it only
  changes how finely the already-paid-for work is divided.
- The old zero was not a discount: on the Triton path the upstream had
  already inferred text-by-text up to the failure point
  (client.py:354), so that work happened and nobody was charged for it.
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

    @property
    def batch_size(self) -> int:
        """Max texts per request. See ``WorkerSettings.embedding_batch_size``."""
        return max(1, int(self._settings.embedding_batch_size))

    async def embed(
        self,
        texts: list[str],
        *,
        user_id: int | None = None,
    ) -> list[list[float]]:
        """Return one vector per input text in the same order.

        The input is partitioned into consecutive slices of
        ``batch_size`` — every text lands in exactly one batch, in
        order. On a run that *succeeds*, that partition is what keeps
        the bill unchanged: CSP meters a request as ``sum(per-text)``
        (proxy/service.py), so a sum over the batches equals the sum
        over the unbatched list.

        That equality is **success-path only**. A run that fails part
        way has already been billed for the batches that succeeded,
        where the unbatched shape would have billed nothing — see the
        module docstring for the measured numbers and the bound.

        On the first failing batch this raises and stops; see the module
        docstring for the partial-failure contract.
        """
        if not texts:
            return []

        size = self.batch_size
        batch_count = (len(texts) + size - 1) // size
        vectors: list[list[float]] = []
        for batch_index in range(batch_count):
            offset = batch_index * size
            batch = texts[offset : offset + size]
            try:
                vectors.extend(await self._embed_batch(batch))
            except EmbedError as e:
                raise self._locate_failure(
                    e,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    offset=offset,
                    batch_len=len(batch),
                    total=len(texts),
                ) from e

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

    @staticmethod
    def _locate_failure(
        error: EmbedError,
        *,
        batch_index: int,
        batch_count: int,
        offset: int,
        batch_len: int,
        total: int,
    ) -> EmbedError:
        """Re-raise ``error`` saying WHICH slice of the document died.

        A rebuilt error rather than a mutated one: ``IngestionError``
        freezes ``str(self)`` in ``__post_init__``, so editing
        ``user_message`` in place would leave the log line saying
        something the details contradict.

        ``code`` / ``retryable`` / ``severity`` are carried over
        untouched — the batch a failure happened in says nothing about
        whether retrying it can help, and the worker's retry policy
        reads ``retryable``.
        """
        end = offset + batch_len
        details = dict(error.details)
        details.update(
            {
                "batch_index": batch_index,
                "batch_count": batch_count,
                # Half-open [start, end) over the ORIGINAL text list.
                "failed_range": [offset, end],
                # Chunk-local keys such as ``index`` refer to a position
                # inside the failed batch; add this to get the document
                # position.
                "embedded_before_failure": offset,
                "input_total": total,
            }
        )
        suffix = (
            f"(第 {batch_index + 1}/{batch_count} 批失敗,對應第 "
            f"{offset}–{end - 1} 段文字;前 {offset} 段已送出但不會被索引)"
        )
        return EmbedError(
            code=error.code,
            retryable=error.retryable,
            severity=error.severity,
            user_message=f"{error.user_message} {suffix}".strip(),
            details=details,
        )

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One POST. ``texts`` is already bounded by ``batch_size``."""
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

        return vectors

    async def close(self) -> None:
        await self._client.aclose()
