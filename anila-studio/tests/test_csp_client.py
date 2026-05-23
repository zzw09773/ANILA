"""Tests for ``app.clients.csp_client``.

The csp client is a thin async wrapper around csp's HTTP API. Tests use
``respx`` to mock outgoing httpx calls so we exercise the full request /
response shaping pipeline without booting csp.

Coverage targets:
- Happy paths for all 5 functions
- 4xx → typed exceptions (no retry)
- 5xx → exhaust retry budget then raise CspServerError
- Timeout / network error → retry, succeed on subsequent attempt
- Bearer header is forwarded verbatim on every request
- ``document_ids=None`` does not appear in the JSON body
- Empty result lists pass through unchanged

All tests share a fixed ``BEARER`` constant. CSP_BASE_URL defaults to
``http://csp:8000`` per ``app/config.py`` so absolute URLs in respx
matchers must match that.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.clients.csp_client import (
    ChunkHit,
    CollectionMeta,
    CspClientError,
    CspForbiddenError,
    CspNotFoundError,
    CspServerError,
    CspUnauthorizedError,
    ImageHit,
    fetch_image_blob,
    get_collection,
    proxy_chat_completions,
    search_chunks,
    search_images,
)
from app.config import settings


BEARER = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.placeholder.signature"
BASE = settings.CSP_BASE_URL  # "http://csp:8000"


# ── get_collection ──────────────────────────────────────────────────────────


@respx.mock
async def test_get_collection_happy() -> None:
    route = respx.get(f"{BASE}/api/ingestion/collections/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "name": "Quarterly Reports",
                "description": "...",
                "chunking_config": {},
                "embedding_model": "nv-embed-v2",
                "embedding_dim": 4000,
                "status": "active",
                "document_count": 5,
                "chunk_count": 123,
                "bytes_stored": 4567,
                "created_by": 7,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        )
    )

    meta = await get_collection(42, bearer=BEARER)

    assert isinstance(meta, CollectionMeta)
    assert meta.id == 42
    assert meta.name == "Quarterly Reports"
    assert meta.embedding_model == "nv-embed-v2"
    assert meta.embedding_dim == 4000
    assert meta.status == "active"
    assert meta.created_by == 7

    # Bearer + path verification
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == f"Bearer {BEARER}"


@respx.mock
async def test_get_collection_404_raises_not_found() -> None:
    respx.get(f"{BASE}/api/ingestion/collections/999").mock(
        return_value=httpx.Response(404, json={"detail": "not found"})
    )

    with pytest.raises(CspNotFoundError):
        await get_collection(999, bearer=BEARER)


@respx.mock
async def test_get_collection_403_raises_forbidden() -> None:
    respx.get(f"{BASE}/api/ingestion/collections/77").mock(
        return_value=httpx.Response(403, json={"detail": "not yours"})
    )

    with pytest.raises(CspForbiddenError):
        await get_collection(77, bearer=BEARER)


# ── search_chunks ───────────────────────────────────────────────────────────


@respx.mock
async def test_search_chunks_happy_returns_flattened_list() -> None:
    route = respx.post(
        f"{BASE}/api/ingestion/collections/5/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": "what is rag",
                "embedding_model": "nv-embed-v2",
                "embedding_dim": 4000,
                "results": [
                    {
                        "chunk_id": 1,
                        "document_id": 10,
                        "filename": "rag.pdf",
                        "chunk_key": "rag.pdf::0",
                        "content": "Retrieval Augmented Generation...",
                        "score": 0.92,
                        "metadata": {"page": 1},
                        "parent_chunk_id": None,
                        "parent_content": None,
                        "chunk_type": "leaf",
                        "chunk_level": 0,
                    },
                    {
                        "chunk_id": 2,
                        "document_id": 10,
                        "filename": "rag.pdf",
                        "chunk_key": "rag.pdf::1",
                        "content": "RAG combines retrieval...",
                        "score": 0.81,
                        "metadata": {"page": 2},
                        "parent_chunk_id": 99,
                        "parent_content": "Parent section text",
                        "chunk_type": "leaf",
                        "chunk_level": 1,
                    },
                ],
            },
        )
    )

    hits = await search_chunks(5, "what is rag", top_k=5, bearer=BEARER)

    assert isinstance(hits, list)
    assert len(hits) == 2
    assert all(isinstance(h, ChunkHit) for h in hits)
    assert hits[0].chunk_id == 1
    assert hits[0].score == 0.92
    assert hits[0].parent_chunk_id is None
    assert hits[1].parent_chunk_id == 99
    assert hits[1].parent_content == "Parent section text"

    # Verify body shape
    body = route.calls.last.request.content
    import json
    parsed = json.loads(body)
    assert parsed["query"] == "what is rag"
    assert parsed["top_k"] == 5
    assert parsed["min_score"] == 0.0
    # document_ids=None → key MUST NOT be present
    assert "document_ids" not in parsed


@respx.mock
async def test_search_chunks_empty_results_passes_through() -> None:
    respx.post(f"{BASE}/api/ingestion/collections/5/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "query": "nothing matches",
                "embedding_model": "nv-embed-v2",
                "embedding_dim": 4000,
                "results": [],
            },
        )
    )

    hits = await search_chunks(5, "nothing matches", bearer=BEARER)
    assert hits == []


@respx.mock
async def test_search_chunks_document_ids_included_when_set() -> None:
    route = respx.post(
        f"{BASE}/api/ingestion/collections/5/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": "q",
                "embedding_model": "nv-embed-v2",
                "embedding_dim": 4000,
                "results": [],
            },
        )
    )

    await search_chunks(
        5,
        "q",
        document_ids=[10, 11],
        bearer=BEARER,
    )

    import json
    parsed = json.loads(route.calls.last.request.content)
    assert parsed["document_ids"] == [10, 11]


# ── search_images ───────────────────────────────────────────────────────────


@respx.mock
async def test_search_images_happy() -> None:
    respx.post(f"{BASE}/api/ingestion/collections/3/images/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "query": "diagram of pipeline",
                "embedding_model": "nv-embed-v2",
                "embedding_dim": 4000,
                "results": [
                    {
                        "image_id": 501,
                        "document_id": 10,
                        "page": 4,
                        "storage_path": "u7/d10/img_501.png",
                        "mime": "image/png",
                        "caption": "Diagram of the pipeline",
                        "filename": "deck.pdf",
                        "score": 0.88,
                    }
                ],
            },
        )
    )

    hits = await search_images(3, "diagram of pipeline", bearer=BEARER)

    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, ImageHit)
    assert h.image_id == 501
    assert h.mime == "image/png"
    assert h.score == 0.88


@respx.mock
async def test_search_images_5xx_retries_three_times_then_raises() -> None:
    route = respx.post(
        f"{BASE}/api/ingestion/collections/3/images/search"
    ).mock(return_value=httpx.Response(503, text="upstream stale"))

    with pytest.raises(CspServerError):
        await search_images(3, "anything", bearer=BEARER)

    # Initial call + 3 retries = 4 total attempts.
    assert route.call_count == 4


# ── fetch_image_blob ────────────────────────────────────────────────────────


@respx.mock
async def test_fetch_image_blob_happy() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    respx.get(f"{BASE}/api/ingestion/images/501/blob").mock(
        return_value=httpx.Response(
            200,
            content=payload,
            headers={"content-type": "image/png"},
        )
    )

    blob, mime = await fetch_image_blob(501, bearer=BEARER)
    assert blob == payload
    assert mime == "image/png"


@respx.mock
async def test_fetch_image_blob_404_raises_not_found() -> None:
    respx.get(f"{BASE}/api/ingestion/images/999/blob").mock(
        return_value=httpx.Response(404, json={"detail": "Image 999 not found"})
    )

    with pytest.raises(CspNotFoundError):
        await fetch_image_blob(999, bearer=BEARER)


# ── proxy_chat_completions ─────────────────────────────────────────────────


@respx.mock
async def test_proxy_chat_completions_happy() -> None:
    upstream_payload = {
        "id": "cmpl-xyz",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        # csp adds billing metadata; client must pass through unchanged.
        "anila_billing": {"cost_usd": 0.00012},
    }
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=upstream_payload)
    )

    result = await proxy_chat_completions(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.2,
        max_tokens=64,
        bearer=BEARER,
    )

    # Full pass-through including custom billing field
    assert result == upstream_payload
    assert result["anila_billing"]["cost_usd"] == 0.00012


@respx.mock
async def test_proxy_chat_completions_401_raises_unauthorized_no_retry() -> None:
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"detail": "token expired"})
    )

    with pytest.raises(CspUnauthorizedError):
        await proxy_chat_completions(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            bearer=BEARER,
        )

    # 401 must NOT trigger retry — a stale JWT will not become valid.
    assert route.call_count == 1


@respx.mock
async def test_proxy_chat_completions_optional_fields_omitted_when_none() -> None:
    """temperature / max_tokens / response_format=None should be dropped
    from the request body so we don't override OpenAI defaults."""
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "choices": [],
            },
        )
    )

    await proxy_chat_completions(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        bearer=BEARER,
    )

    import json
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "gpt-4o"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert "response_format" not in body


# ── Retry on timeout / network error ────────────────────────────────────────


@respx.mock
async def test_timeout_then_success_retries_until_pass() -> None:
    """First call raises ReadTimeout, second returns 200. Expect retry."""
    route = respx.get(f"{BASE}/api/ingestion/collections/1").mock(
        side_effect=[
            httpx.ReadTimeout("simulated upstream stall"),
            httpx.Response(
                200,
                json={
                    "id": 1,
                    "name": "ok",
                    "description": None,
                    "chunking_config": {},
                    "embedding_model": "nv-embed-v2",
                    "embedding_dim": 4000,
                    "status": "active",
                    "document_count": 0,
                    "chunk_count": 0,
                    "bytes_stored": 0,
                    "created_by": 1,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            ),
        ]
    )

    meta = await get_collection(1, bearer=BEARER)
    assert meta.id == 1
    assert route.call_count == 2


# ── Bearer header always present ────────────────────────────────────────────


@respx.mock
async def test_bearer_header_always_forwarded() -> None:
    """Every endpoint must send Authorization: Bearer <token>."""
    routes = [
        respx.get(f"{BASE}/api/ingestion/collections/1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 1,
                    "name": "n",
                    "description": None,
                    "chunking_config": {},
                    "embedding_model": "e",
                    "embedding_dim": 1,
                    "status": "active",
                    "document_count": 0,
                    "chunk_count": 0,
                    "bytes_stored": 0,
                    "created_by": 1,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        ),
        respx.post(f"{BASE}/api/ingestion/collections/1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "query": "q",
                    "embedding_model": "e",
                    "embedding_dim": 1,
                    "results": [],
                },
            )
        ),
        respx.post(f"{BASE}/api/ingestion/collections/1/images/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "query": "q",
                    "embedding_model": "e",
                    "embedding_dim": 1,
                    "results": [],
                },
            )
        ),
        respx.get(f"{BASE}/api/ingestion/images/1/blob").mock(
            return_value=httpx.Response(
                200, content=b"x", headers={"content-type": "image/jpeg"}
            )
        ),
        respx.post(f"{BASE}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"id": "x", "object": "chat.completion", "choices": []},
            )
        ),
    ]

    await get_collection(1, bearer=BEARER)
    await search_chunks(1, "q", bearer=BEARER)
    await search_images(1, "q", bearer=BEARER)
    await fetch_image_blob(1, bearer=BEARER)
    await proxy_chat_completions(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        bearer=BEARER,
    )

    for r in routes:
        assert r.called
        assert (
            r.calls.last.request.headers["authorization"]
            == f"Bearer {BEARER}"
        )


# ── Sanity check: error hierarchy ───────────────────────────────────────────


def test_error_hierarchy() -> None:
    """All typed errors descend from CspClientError so callers can catch
    by base class when they don't care about the specific status."""
    assert issubclass(CspUnauthorizedError, CspClientError)
    assert issubclass(CspForbiddenError, CspClientError)
    assert issubclass(CspNotFoundError, CspClientError)
    assert issubclass(CspServerError, CspClientError)
