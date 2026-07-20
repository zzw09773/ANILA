"""Unit tests for ingestion_worker.embedder.Embedder.embed.

All HTTP is mocked with respx — no network is touched. The Embedder posts
an OpenAI-compatible request to ``{base_url}/embeddings``; with
``embedding_base_url="http://embed.test/v1"`` httpx resolves the call to
``http://embed.test/v1/embeddings`` (the leading-slash path is joined onto
the base path, see httpx URL merge rules).

Covered behaviours:
- empty input short-circuits to ``[]`` (no HTTP call)
- happy path with client-side truncation (4096-d native -> 4000-d schema)
- HTTP non-200 -> E_EMBED_MODEL_DOWN (retryable)
- httpx.TimeoutException -> EmbedError.timeout (E_EMBED_TIMEOUT)
- malformed payload (missing data[].embedding) -> E_EMBED_MODEL_DOWN
- output/input count mismatch -> E_EMBED_MODEL_DOWN (alignment broken)
- short vector (fewer dims than schema) -> E_EMBED_DIM_MISMATCH
"""

from __future__ import annotations

import httpx
import pytest
import respx

from anila_core.ingestion.errors import EmbedError
from anila_core.memory import EMBED_DIM, EMBED_NATIVE_DIM

from ingestion_worker.embedder import Embedder
from ingestion_worker.settings import WorkerSettings


EMBED_URL = "http://embed.test/v1/embeddings"


def _make_settings(**overrides) -> WorkerSettings:
    """Construct a WorkerSettings with deterministic embedding endpoint.

    All other fields keep their pydantic defaults. ``_env_file=None``
    prevents picking up a stray local .env during the test run.
    """
    base = {
        "embedding_base_url": "http://embed.test/v1",
        "embedding_model": "test-model",
        "embedding_api_key": "test-key",
        "embedding_dim": 4000,
        "embedding_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return WorkerSettings(_env_file=None, **base)


def _payload(vectors: list[list[float]]) -> dict:
    """OpenAI-compatible embeddings response envelope."""
    return {"data": [{"embedding": v} for v in vectors]}


def _vector(value: float, dimension: int = EMBED_DIM) -> list[float]:
    return [value] * dimension


async def test_empty_input_returns_empty_without_http():
    """No texts -> [] and the endpoint is never called."""
    settings = _make_settings()
    embedder = Embedder(settings)
    try:
        with respx.mock:
            route = respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json=_payload([]))
            )
            result = await embedder.embed([])
        assert result == []
        assert route.call_count == 0
    finally:
        await embedder.close()


async def test_happy_path_truncates_to_schema_dim():
    """4096-d native vectors are truncated client-side to 4000-d."""
    settings = _make_settings(embedding_dim=4000)
    embedder = Embedder(settings)
    native = [float(i % 7) for i in range(EMBED_NATIVE_DIM)]
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json=_payload([native]))
            )
            result = await embedder.embed(["hello world"])
        assert len(result) == 1
        assert len(result[0]) == EMBED_DIM
        # Truncation drops the trailing 96 dims, preserving the prefix.
        assert result[0] == native[:EMBED_DIM]
    finally:
        await embedder.close()


async def test_happy_path_exact_dim_passthrough():
    """Vectors already at the schema dim pass through unchanged."""
    settings = _make_settings()
    embedder = Embedder(settings)
    vecs = [_vector(0.1), _vector(0.2)]
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json=_payload(vecs))
            )
            result = await embedder.embed(["a", "b"])
        assert result == vecs
    finally:
        await embedder.close()


async def test_request_body_shape():
    """The request carries the configured model and the input list."""
    settings = _make_settings(embedding_model="my-model")
    embedder = Embedder(settings)
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(200, json=_payload([_vector(1.0)]))

    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(side_effect=_handler)
            await embedder.embed(["only-text"])
        assert captured["model"] == "my-model"
        assert captured["input"] == ["only-text"]
    finally:
        await embedder.close()


async def test_non_200_raises_model_down_retryable():
    """HTTP 503 -> E_EMBED_MODEL_DOWN, retryable, with status detail."""
    settings = _make_settings()
    embedder = Embedder(settings)
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(503, text="upstream unavailable")
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["x"])
        err = excinfo.value
        assert err.code == "E_EMBED_MODEL_DOWN"
        assert err.retryable is True
        assert err.details["status_code"] == 503
        assert "upstream unavailable" in err.details["body_snippet"]
    finally:
        await embedder.close()


async def test_timeout_raises_embed_timeout():
    """httpx.TimeoutException is mapped to EmbedError.timeout."""
    settings = _make_settings()
    embedder = Embedder(settings)
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                side_effect=httpx.TimeoutException("timed out")
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["x"])
        err = excinfo.value
        assert err.code == "E_EMBED_TIMEOUT"
        assert err.retryable is True
        assert err.details["timeout_s"] == settings.embedding_timeout_seconds
    finally:
        await embedder.close()


async def test_malformed_payload_missing_embedding_raises_model_down():
    """A payload whose data items lack 'embedding' -> E_EMBED_MODEL_DOWN."""
    settings = _make_settings()
    embedder = Embedder(settings)
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json={"data": [{"oops": [1, 2]}]})
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["x"])
        err = excinfo.value
        assert err.code == "E_EMBED_MODEL_DOWN"
        # This shape error is non-retryable (bad payload, not transient).
        assert err.retryable is False
    finally:
        await embedder.close()


async def test_malformed_payload_missing_data_key_raises_model_down():
    """A payload entirely missing the 'data' key -> E_EMBED_MODEL_DOWN."""
    settings = _make_settings()
    embedder = Embedder(settings)
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json={"unexpected": "shape"})
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["x"])
        assert excinfo.value.code == "E_EMBED_MODEL_DOWN"
        assert excinfo.value.retryable is False
    finally:
        await embedder.close()


async def test_count_mismatch_raises_model_down():
    """Fewer vectors than inputs -> E_EMBED_MODEL_DOWN (alignment broken)."""
    settings = _make_settings()
    embedder = Embedder(settings)
    try:
        with respx.mock:
            # Two inputs, but the endpoint returns only one vector.
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json=_payload([_vector(1.0)]))
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["a", "b"])
        err = excinfo.value
        assert err.code == "E_EMBED_MODEL_DOWN"
        assert err.retryable is False
        assert err.details["input_count"] == 2
        assert err.details["output_count"] == 1
    finally:
        await embedder.close()


async def test_out_of_order_data_index_realigns_vectors():
    """respx OpenAI-相容端點回傳的 ``data[]`` 不保證與 ``input`` 同序 —— 只保證
    每個 item 帶的 ``index`` 欄位對得回原始 input 位置。這裡把 3 筆輸入的
    embedding 故意用倒序回傳（index=2,0,1 的陣列順序),驗證對齊後仍照
    input 順序（依 index 排序,而非依 data[] 陣列順序）。
    """
    settings = _make_settings()
    embedder = Embedder(settings)
    # texts = ["a", "b", "c"] -> 對應 index 0, 1, 2；data[] 陣列本身倒序/亂序。
    first = _vector(1.0)
    second = _vector(2.0)
    third = _vector(3.0)
    payload = {
        "data": [
            {"embedding": third, "index": 2},
            {"embedding": first, "index": 0},
            {"embedding": second, "index": 1},
        ]
    }
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=payload))
            result = await embedder.embed(["a", "b", "c"])
        # 依 index 對齊：a->index0, b->index1, c->index2，不是 data[] 陣列順序。
        assert result == [first, second, third]
    finally:
        await embedder.close()


async def test_missing_index_falls_back_to_array_order():
    """端點不回 index 欄位時退回既有行為（依 data[] 陣列順序對齊)。"""
    settings = _make_settings()
    embedder = Embedder(settings)
    vecs = [_vector(1.0), _vector(2.0), _vector(3.0)]
    payload = _payload(vecs)
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(return_value=httpx.Response(200, json=payload))
            result = await embedder.embed(["a", "b", "c"])
        assert result == vecs
    finally:
        await embedder.close()


async def test_short_vector_raises_dim_mismatch():
    """A vector shorter than the schema dim -> E_EMBED_DIM_MISMATCH."""
    settings = _make_settings(embedding_dim=4000)
    embedder = Embedder(settings)
    short = [0.0] * 1536  # wrong model: 1536-d instead of 4000-d
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json=_payload([short]))
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["x"])
        err = excinfo.value
        assert err.code == "E_EMBED_DIM_MISMATCH"
        assert err.retryable is False
        assert err.details["got"] == 1536
        assert err.details["expected"] == 4000
        assert err.details["index"] == 0
    finally:
        await embedder.close()


@pytest.mark.parametrize("dimension", [EMBED_DIM - 1, EMBED_NATIVE_DIM + 512])
async def test_unexpected_embedding_dimension_is_never_returned(dimension):
    """3999/4608-d responses fail before a caller can write the vector."""
    embedder = Embedder(_make_settings())
    try:
        with respx.mock:
            respx.post(EMBED_URL).mock(
                return_value=httpx.Response(200, json=_payload([_vector(0.5, dimension)]))
            )
            with pytest.raises(EmbedError) as excinfo:
                await embedder.embed(["x"])
        err = excinfo.value
        assert err.code == "E_EMBED_DIM_MISMATCH"
        assert err.retryable is False
        assert err.details == {
            "got": dimension,
            "expected": EMBED_DIM,
            "native": EMBED_NATIVE_DIM,
            "index": 0,
        }
    finally:
        await embedder.close()
