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

import pytest

from app.api import proxy
from app.models.user_memory import UserFact
from app.services import memory_service
from app.services.memory_service import (
    RetrievedChunk,
    _format_block,
    parse_extraction_response,
)


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
    assert _format_block([], []) is None


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
    block = _format_block([], chunks)
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
async def test_inject_memory_prepends_to_existing_system_message(monkeypatch):
    """When the client already sends a system message, the memory
    block prepends to its content — it doesn't replace it. Replacing
    would silently drop client-side instructions like the ZHTW
    directive we ship from ANILALM.

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
    assert body["messages"][0]["content"].startswith("MEMORY_BLOCK_SENTINEL")
    assert "client-side rules go here" in body["messages"][0]["content"]
    # User message untouched.
    assert body["messages"][1] == {"role": "user", "content": "hello"}
