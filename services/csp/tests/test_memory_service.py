"""Unit tests for the user-scoped memory service.

Pure-function coverage only — the SQL retrieval path uses pgvector's
``halfvec`` + ``<=>`` operator which SQLite (the test backend) doesn't
implement, so similarity search and persistence are tested as part of
the docker-compose smoke flow rather than here.

Each test exercises one boundary of the service contract:

* ``parse_extraction_response`` — hardening against LLM drift
  (placeholder echoing, malformed JSON, out-of-range confidence)
* ``_format_block`` — system-prompt block formatting + encryption
  flag surfacing
* ``proxy._coerce_conversation_id`` — header-to-FK normalisation
* ``proxy._inject_memory`` — message-array mutation rules

These run in ~1 ms each on the SQLite fixture and don't require any
mocked HTTP — they protect the rules whose violation has historically
been a source of "memory feels weird" bugs (see CLAUDE.md feedback
about agent name hallucination from in-context examples).
"""
from __future__ import annotations

import logging

import pytest

from app.api import proxy
from app.models.user_memory import UserFact
from app.services import memory_service
from app.services.memory_service import (
    RetrievedChunk,
    _format_block,
    parse_extraction_response,
)

#: 見 ``test_memory_block_format.py``：上限是必填參數，這裡不測截斷。
_NO_TRUNCATION = 100_000
from anila_core.security import ENDPOINT_KIND_MODEL


# ── parse_extraction_response ────────────────────────────────────────────────


def test_parse_extraction_strips_placeholder_template_echo():
    """Critical guard: model occasionally regurgitates the prompt's
    `<key>` / `<value>` placeholder text instead of real extractions.
    Those rows must never reach the database — we'd be inserting
    schema metadata as user facts.
    """
    raw = (
        '[{"key": "<fact_category>", "value": "<concrete_value>", '
        '"confidence": 0.5}, '
        '{"key": "actual_field", "value": "actual_data", "confidence": 0.9}]'
    )
    facts = parse_extraction_response(raw)
    assert len(facts) == 1
    assert facts[0]["key"] == "actual_field"
    assert facts[0]["value"] == "actual_data"


def test_parse_extraction_handles_preamble_and_clamps_confidence():
    """Two assertions in one because they exercise the same path.

    The LLM may emit a `<think>...</think>` reasoning block before the
    JSON array; the regex extracts the array regardless. Confidence
    out of [0.0, 1.0] gets clamped instead of raising — a rejection
    here would silently lose otherwise-valid extractions.
    """
    raw = (
        "<think>let me think about this</think>\n"
        '[{"key": "k1", "value": "v1", "confidence": 1.5}, '
        '{"key": "k2", "value": "v2", "confidence": -0.3}, '
        '{"key": "k3", "value": "v3", "confidence": "not-a-number"}]'
    )
    facts = parse_extraction_response(raw)
    assert [f["confidence"] for f in facts] == [1.0, 0.0, 1.0]


def test_parse_extraction_rejects_non_array_and_garbage():
    assert parse_extraction_response("not json") == []
    assert parse_extraction_response('{"key": "single"}') == []
    assert parse_extraction_response("[]") == []
    # Items missing required fields are dropped, but the array is valid.
    raw = '[{"key": "ok", "value": "v"}, {"only_key": "no_value"}]'
    assert len(parse_extraction_response(raw)) == 1


# ── _format_block ────────────────────────────────────────────────────────────


def test_format_block_returns_none_when_nothing_to_inject():
    """No facts + no chunks → no system-prompt mutation.

    Skips the ``db`` fixture (and its pre-existing JSONB / SQLite
    metadata collision) because ``_format_block`` is pure.
    """
    assert _format_block([], [], max_chunk_chars=_NO_TRUNCATION) is None


def test_format_block_marks_encrypted_chunks_with_visible_tag():
    """Encrypted retrieval must be visibly tagged so the LLM (and
    later the UI) can render appropriate provenance. Without the tag,
    classified content reads identically to public content in the
    injected block — exactly the leak P3 inheritance is meant to
    prevent on the consuming side.
    """
    chunks = [
        RetrievedChunk(
            id=1,
            conversation_id=10,
            role="user",
            content="public content",
            cosine=0.9,
            is_encrypted=False,
        ),
        RetrievedChunk(
            id=2,
            conversation_id=11,
            role="assistant",
            content="classified content",
            cosine=0.8,
            is_encrypted=True,
        ),
    ]
    block = _format_block([], chunks, max_chunk_chars=_NO_TRUNCATION)
    assert block is not None
    assert "(加密來源)" in block
    # Public chunk gets no tag.
    assert "user (similarity 0.90)" in block


# ── proxy._coerce_conversation_id ────────────────────────────────────────────


def test_coerce_conversation_id_handles_legacy_and_missing_values():
    """Header value goes int → int, junk → None, missing → None.

    A non-coercible value disables the writer (FK is integer NOT NULL)
    but still allows the reader. This test pins the contract so a
    future "make it strict" refactor doesn't accidentally start
    raising on UUID-shaped headers from old clients.
    """
    assert proxy._coerce_conversation_id("42") == 42
    assert proxy._coerce_conversation_id("not-an-int") is None
    assert proxy._coerce_conversation_id(None) is None
    assert proxy._coerce_conversation_id("") is None


# ── proxy._inject_memory message mutation ────────────────────────────────────


def test_memory_read_result_encryption_inherited_property():
    """The proxy P3 latch keys off this single property — pin its
    semantics so a refactor that splits the chunk struct doesn't
    silently break the inheritance check.
    """
    from app.services.memory_service import MemoryReadResult, RetrievedChunk

    safe = RetrievedChunk(
        id=1, conversation_id=1, role="user",
        content="x", cosine=0.9, is_encrypted=False,
    )
    classified = RetrievedChunk(
        id=2, conversation_id=2, role="assistant",
        content="y", cosine=0.8, is_encrypted=True,
    )
    assert MemoryReadResult(block=None, facts_count=0, chunks=[]).encryption_inherited is False
    assert MemoryReadResult(block=None, facts_count=0, chunks=[safe]).encryption_inherited is False
    assert MemoryReadResult(block=None, facts_count=0, chunks=[safe, classified]).encryption_inherited is True


@pytest.mark.asyncio
async def test_inject_memory_appends_to_existing_system_message(monkeypatch):
    """When the client already sends a system message, the memory
    block is appended after its content — it doesn't replace it, and
    (2026-09-02, harness §6-1) it no longer goes in front of it: the
    caller's static preamble must stay the byte-identical prefix.

    Patches ``build_memory_block`` so no DB is needed — the test is
    about the proxy-side message-array merge logic.
    """
    body = {
        "model": "gemma4",
        "messages": [
            {"role": "system", "content": "client-side rules go here"},
            {"role": "user", "content": "hello"},
        ],
    }

    async def fake_build(*args, **kwargs):
        return memory_service.MemoryReadResult(
            block="MEMORY_BLOCK_SENTINEL",
            facts_count=1,
            chunks=[],
        )

    monkeypatch.setattr(memory_service, "build_memory_block", fake_build)

    result = await proxy._inject_memory(
        None, user_id=1, body=body, exclude_conversation_id=None
    )
    assert result is not None
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"].startswith("client-side rules go here")
    assert body["messages"][0]["content"].endswith("MEMORY_BLOCK_SENTINEL")
    # User message untouched.
    assert body["messages"][1] == {"role": "user", "content": "hello"}


# ── _resolve_extraction_target (fact-extraction model fallback) ───────────────


def _add_llm(db, name, url, *, is_active=True):
    from app.models.model_registry import ModelRegistry

    db.add(
        ModelRegistry(
            name=name,
            display_name=name,
            model_type="llm",
            endpoint_url=url,
            is_active=is_active,
        )
    )
    db.commit()


def test_resolve_endpoint_rejects_inactive_model(db):
    """An inactive row is not an endpoint resolution result."""
    _add_llm(db, "disabled-llm", "http://disabled:8000", is_active=False)

    with pytest.raises(RuntimeError, match="row not found"):
        memory_service._resolve_endpoint(db, "disabled-llm", "llm")


def test_resolve_extraction_target_prefers_configured_model(db, monkeypatch):
    """When MEMORY_LLM_MODEL is a registered active LLM, use it verbatim."""
    _add_llm(db, "gemma4", "http://gemma:8000")
    _add_llm(db, "openai/gpt-oss-20b", "http://gpt:8000")
    monkeypatch.setattr(memory_service, "_LLM_MODEL_NAME", "gemma4")
    assert memory_service._resolve_extraction_target(db) == (
        "gemma4",
        "http://gemma:8000",
    )


def test_resolve_extraction_target_falls_back_to_available_llm(db, monkeypatch):
    """Air-gap case: configured gemma4 isn't registered (only gpt-oss is) →
    extraction falls back to the available LLM instead of disabling itself.
    """
    _add_llm(db, "openai/gpt-oss-20b", "http://gpt:8000/")  # note trailing slash
    monkeypatch.setattr(memory_service, "_LLM_MODEL_NAME", "gemma4")
    target = memory_service._resolve_extraction_target(db)
    assert target == ("openai/gpt-oss-20b", "http://gpt:8000")  # rstripped


def test_resolve_extraction_target_falls_back_from_inactive_configured_model(
    db, monkeypatch, caplog
):
    """A deactivated configured LLM is treated like a missing target."""
    _add_llm(db, "disabled-llm", "http://disabled:8000", is_active=False)
    _add_llm(db, "active-llm", "http://active:8000")
    monkeypatch.setattr(memory_service, "_LLM_MODEL_NAME", "disabled-llm")

    with caplog.at_level(logging.WARNING, logger=memory_service.__name__):
        target = memory_service._resolve_extraction_target(db)

    assert target == ("active-llm", "http://active:8000")
    assert any(
        "falling back to 'active-llm'" in record.getMessage()
        for record in caplog.records
    )


def test_resolve_extraction_target_none_when_no_active_llm(db, monkeypatch):
    """No active LLM at all → None (caller disables extraction, logs)."""
    monkeypatch.setattr(memory_service, "_LLM_MODEL_NAME", "gemma4")
    assert memory_service._resolve_extraction_target(db) is None


@pytest.mark.asyncio
async def test_extract_facts_skips_http_when_every_llm_is_inactive(
    db, monkeypatch, caplog
):
    """No inactive registry row may leak through to the HTTP call."""
    _add_llm(db, "disabled-llm", "http://disabled:8000", is_active=False)
    monkeypatch.setattr(memory_service, "_LLM_MODEL_NAME", "disabled-llm")

    class _NoHTTPClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("inactive model reached the HTTP client")

    monkeypatch.setattr(memory_service.httpx, "AsyncClient", _NoHTTPClient)

    with caplog.at_level(logging.WARNING, logger=memory_service.__name__):
        assert await memory_service._extract_facts(db, "a sufficiently long turn") == []

    assert any(
        "no active LLM registered" in record.getMessage()
        for record in caplog.records
    )


def test_memory_outbound_guard_uses_model_endpoint_kind(monkeypatch):
    """Memory embeddings send MODEL_GATEWAY_API_KEY, so the outbound guard
    must use the model endpoint class instead of the generic HTTP relaxation.
    """
    calls: list[tuple[str, str]] = []

    def fake_validate(url, *, endpoint_kind):
        calls.append((url, endpoint_kind))

    monkeypatch.setattr(memory_service, "validate_outbound_url", fake_validate)

    memory_service._guard_outbound("https://embed.example/v1")

    assert calls == [("https://embed.example/v1", ENDPOINT_KIND_MODEL)]
