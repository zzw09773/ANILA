"""Phase 3 acceptance wiring smoke tests.

Verifies that the studio pipeline's three external touch points reach
the right HTTP destinations after the csp/backend extraction:

1. RAG → csp ``/api/ingestion/collections/{id}/search``
2. LLM → csp ``/v1/chat/completions``
3. PPTX → ``RENDERER_BASE_URL/render``

These are NOT full e2e (the complete pipeline takes ~60s real wall-clock
with FLUX). Instead each test exercises the smallest helper that owns the
HTTP call path so a regression at the csp/backend boundary surfaces in
seconds. Comprehensive end-to-end coverage stays in Phase 4 e2e tests.

Notes
-----
* Uses ``respx`` to intercept httpx; csp_client is the only call site that
  produces real HTTPX requests so we don't need to mock anything else.
* The studio endpoint's POST handler kicks off ``_run_pipeline`` as an
  asyncio.Task (job-based). Testing the full task is brittle (timing,
  mark_done assertions) — we patch ``_run_pipeline`` to a no-op coroutine
  and only assert the up-front collection check fired through csp.
* For the LLM + PPTX paths, we call the helper directly with a bearer
  string so we don't pay the cost of standing up the full job manager.
"""
from __future__ import annotations

import base64

import httpx
import pytest
import respx

from app.api import studio as studio_mod
from app.config import settings


# ── Mock csp / renderer URLs (must match config defaults) ───────────────


_CSP = settings.CSP_BASE_URL.rstrip("/")
_RENDERER = studio_mod.RENDERER_BASE_URL.rstrip("/")


def _collection_payload() -> dict:
    """Minimal csp ``CollectionResponse`` shape that ``get_collection``
    parses into ``CollectionMeta``. Keep the keys aligned with
    ``csp_client._chunk_hit_from_dict`` peer.
    """
    return {
        "id": 7,
        "name": "smoke-collection",
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "status": "active",
        "created_by": 42,
    }


def _chunk_search_payload() -> dict:
    """Minimal csp search response with two ChunkHit rows."""
    return {
        "results": [
            {
                "chunk_id": 1,
                "document_id": 1,
                "filename": "doc-a.pdf",
                "chunk_key": "page-1#0",
                "content": "Smoke test chunk content #1 — long enough to clear "
                "the studio prompt budget downstream.",
                "score": 0.92,
                "metadata": {},
                "parent_chunk_id": None,
                "parent_content": None,
                "chunk_type": "leaf",
                "chunk_level": 0,
            },
            {
                "chunk_id": 2,
                "document_id": 1,
                "filename": "doc-a.pdf",
                "chunk_key": "page-2#0",
                "content": "Smoke test chunk content #2.",
                "score": 0.78,
                "metadata": {},
                "parent_chunk_id": None,
                "parent_content": None,
                "chunk_type": "leaf",
                "chunk_level": 0,
            },
        ],
    }


def _proxy_chat_payload() -> dict:
    """Minimal OpenAI-shape response csp's proxy returns."""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": '{"ok": true}'},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_wiring_calls_csp_search():
    """``_retrieve_chunks`` must hit csp's collection-search endpoint and
    project the results into the dict shape callers consume. Asserts
    both the collection lookup AND the search call landed on csp.
    """
    with respx.mock(assert_all_called=True, base_url=_CSP) as mock:
        coll_route = mock.get("/api/ingestion/collections/7").mock(
            return_value=httpx.Response(200, json=_collection_payload()),
        )
        search_route = mock.post(
            "/api/ingestion/collections/7/search",
        ).mock(
            return_value=httpx.Response(200, json=_chunk_search_payload()),
        )

        results = await studio_mod._retrieve_chunks(
            bearer="dev-bearer",
            collection_id=7,
            seed_query="smoke topic",
        )

    assert coll_route.called, "collection lookup should hit csp"
    assert search_route.called, "chunk search should hit csp"
    assert len(results) == 2
    # Hit shape matches what the prompt builder downstream expects.
    assert results[0]["filename"] == "doc-a.pdf"
    assert results[0]["chunk_key"] == "page-1#0"
    assert results[0]["score"] == pytest.approx(0.92)
    # csp returned >STUDIO_CONTENT_LIMIT_CHARS-clean content; trunc is a
    # no-op here but the call path is the load-bearing assertion.
    assert "Smoke test chunk content #1" in results[0]["content"]


@pytest.mark.asyncio
async def test_llm_wiring_calls_csp_proxy():
    """``_call_llm_chat`` must reach csp's ``/v1/chat/completions``
    endpoint and forward the model name + messages verbatim. The legacy
    direct-to-vLLM path is gone — every studio LLM call is csp-fronted
    so usage metering / billing land on the right user.
    """
    with respx.mock(assert_all_called=True, base_url=_CSP) as mock:
        proxy_route = mock.post("/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_proxy_chat_payload()),
        )

        content = await studio_mod._call_llm_chat(
            bearer="dev-bearer",
            model_name=studio_mod.SLIDES_LLM_MODEL,
            messages=[{"role": "user", "content": "smoke ping"}],
            temperature=0.3,
        )

    assert proxy_route.called, "LLM call should hit csp proxy"
    assert content == '{"ok": true}'
    # Verify the body we sent included the model + temperature so csp's
    # routing logic gets what it needs.
    sent = proxy_route.calls[0].request
    body = sent.content.decode("utf-8")
    assert studio_mod.SLIDES_LLM_MODEL in body
    # httpx serialises JSON without spaces by default — match the wire form.
    assert '"temperature":0.3' in body
    # Bearer must have been forwarded for csp's auth + billing pipeline.
    assert sent.headers["authorization"] == "Bearer dev-bearer"


@pytest.mark.asyncio
async def test_pptx_wiring_calls_renderer():
    """``_render_pptx`` must POST to ``RENDERER_BASE_URL/render`` with the
    spec JSON and return the binary body the caller streams back to the
    user as a .pptx download.
    """
    fake_pptx = b"PK\x03\x04 smoke-test-bytes"

    # The renderer expects a {"spec": {...}} body and returns a pptx
    # binary with an X-Pptx-Path header pointing to its tmp dir.
    with respx.mock(assert_all_called=True, base_url=_RENDERER) as mock:
        render_route = mock.post("/render").mock(
            return_value=httpx.Response(
                200,
                content=fake_pptx,
                headers={"X-Pptx-Path": "/tmp/smoke.pptx"},
            ),
        )

        spec = studio_mod.SlidesSpec.model_validate(
            {
                "title": "smoke deck",
                # theme None → resolved from legacy palette default.
                "slides": [
                    {
                        "layout_kind": "cover",
                        "title": "smoke deck",
                        "bullets": ["one bullet to satisfy min_length"],
                    },
                ],
            }
        )

        # No images_lookup + no FLUX provider + no llm → hydration step
        # is skipped (cover_hero_ready=False, images_lookup empty).
        # That keeps the test focused on the renderer-side call path.
        pptx_bytes, pptx_path = await studio_mod._render_pptx(
            spec,
            images_lookup=None,
            bearer="dev-bearer",
            deck_base_seed=None,
            llm=None,
            deck_style=None,
        )

    assert render_route.called, "pptx-renderer /render should be called"
    assert pptx_bytes == fake_pptx
    assert pptx_path == "/tmp/smoke.pptx"
