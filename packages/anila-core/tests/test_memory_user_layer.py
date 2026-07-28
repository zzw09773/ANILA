"""Phase 1 unit tests for the user-tenant memory layer in anila-core.

Pure-function coverage only. The MemoryAdapter Protocol is exercised
in CSP integration tests once Phase 2 wires up the storage backend
— here we only verify the contract the storage backend has to
implement is well-typed and the helper functions behave under the
inputs CSP currently observes in production.
"""
from __future__ import annotations

import pytest
from anila_contracts import Classification

from anila_core.memory import long_term as memory_user
from anila_core.memory.long_term import (
    DEFAULT_EMBED_MODEL,
    EMBED_DIM,
    EMBED_NATIVE_DIM,
    MemoryAdapter,
    MemoryReadResult,
    RetrievedChunk,
    UserFactDTO,
    format_transcript_for_extraction,
    parse_extraction_response,
    truncate_embedding,
)


# ── package layout ───────────────────────────────────────────────────────────


def test_namespace_exports_match_documented_surface():
    """The RFC and README reference these symbols. If a refactor
    drops one, downstream importers (CSP PostgresMemoryAdapter,
    future agent-side consumer) silently break — pin it.
    """
    expected = {
        "DEFAULT_EMBED_MODEL",
        "EMBED_DIM",
        "EMBED_NATIVE_DIM",
        "EXTRACTION_SYSTEM_PROMPT",
        "MemoryAdapter",
        "MemoryReadResult",
        "RetrievedChunk",
        "UserFactDTO",
        "format_transcript_for_extraction",
        "parse_extraction_response",
        "truncate_embedding",
    }
    assert expected.issubset(set(memory_user.__all__))


# ── parse_extraction_response ────────────────────────────────────────────────


def test_parse_extraction_strips_placeholder_template_echo():
    """Critical guard ported from CSP P1 tests — small models
    sometimes regurgitate the prompt's `<key>` / `<value>` template
    instead of real extractions. Filter them so schema metadata
    never reaches the database.
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
    """Two assertions on the same path: extractor LLM may emit a
    `<think>` reasoning block before the JSON; confidence values
    out of [0.0, 1.0] are clamped instead of dropping the whole
    fact.
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
    raw = '[{"key": "ok", "value": "v"}, {"only_key": "no_value"}]'
    assert len(parse_extraction_response(raw)) == 1


def test_parse_extraction_truncates_long_keys():
    """`key` is bounded to 120 chars (storage column width).
    A bug here would surface as DB-side string-too-long errors
    much later; cheaper to enforce in the parser.
    """
    long_key = "k" * 200
    raw = f'[{{"key": "{long_key}", "value": "v"}}]'
    facts = parse_extraction_response(raw)
    assert len(facts) == 1
    assert len(facts[0]["key"]) == 120


# ── format_transcript_for_extraction ─────────────────────────────────────────


def test_format_transcript_keeps_role_labels_for_extractor():
    """The extractor LLM keys off the 「使用者」 / 「助理」 labels
    to know which side of the turn to attribute facts to. If the
    format drifts (e.g. drops to plain newlines) extraction
    quality silently degrades.
    """
    out = format_transcript_for_extraction("hi", "hello")
    assert "使用者：hi" in out
    assert "助理：hello" in out


# ── truncate_embedding ───────────────────────────────────────────────────────


def test_truncate_embedding_preserves_storage_dim_passthrough():
    vec = [0.1] * EMBED_DIM
    assert truncate_embedding(vec) == vec


def test_truncate_embedding_drops_tail_from_native():
    vec = list(range(EMBED_NATIVE_DIM))
    out = truncate_embedding(vec)
    assert len(out) == EMBED_DIM
    assert out[-1] == EMBED_DIM - 1  # tail dropped, head intact


def test_truncate_embedding_zero_pads_declared_smaller_dim():
    """A **declared** smaller-dim model adapts via zero-padding:
    cosine-lossless because padded zeros add nothing to dot products or norms.
    """
    vec = [0.5] * 2048
    out = truncate_embedding(vec, pad_from=2048)
    assert len(out) == EMBED_DIM
    assert out[:2048] == vec  # head intact
    assert set(out[2048:]) == {0.0}  # tail is all zeros


def test_truncate_embedding_refuses_undeclared_short_vector():
    """Padding must be opt-in — this is the guard that catches endpoint drift.

    An earlier revision padded any ``0 < n < EMBED_DIM`` unconditionally, which
    meant a misconfigured or silently-swapped endpoint (e.g. a 1536-d model)
    would have its vectors zero-padded into the index: syntactically valid,
    semantically meaningless, and with no error anywhere. The collection
    ``embedding_fingerprint`` cannot catch that — it guards the *declared*
    model identity, not what the endpoint actually serves.
    """
    with pytest.raises(ValueError, match="dim 1536"):
        truncate_embedding([0.5] * 1536)


def test_truncate_embedding_refuses_short_vector_that_is_not_the_declared_dim():
    """Declaring 2048 must not turn into "accept anything short"."""
    with pytest.raises(ValueError, match="dim 1536"):
        truncate_embedding([0.5] * 1536, pad_from=2048)


def test_truncate_embedding_declared_dim_does_not_break_exact_and_native():
    """Declaring a pad dimension must not disturb the two contract dims."""
    assert truncate_embedding([0.1] * EMBED_DIM, pad_from=2048) == [0.1] * EMBED_DIM
    native = truncate_embedding([0.2] * EMBED_NATIVE_DIM, pad_from=2048)
    assert len(native) == EMBED_DIM


def test_truncate_embedding_raises_on_overlong_non_native_dim():
    """Loud failure beats silent corruption: blind truncation of a
    non-Matryoshka model would corrupt retrieval semantics.
    """
    with pytest.raises(ValueError, match="dim 5000"):
        truncate_embedding([0.1] * 5000)


def test_truncate_embedding_raises_on_empty_vector():
    with pytest.raises(ValueError, match="dim 0"):
        truncate_embedding([])


# ── MemoryReadResult.encryption_inherited ────────────────────────────────────


def test_memory_read_result_encryption_inherited_default_empty():
    assert MemoryReadResult(block=None, facts_count=0).encryption_inherited is False


def test_memory_read_result_encryption_inherited_one_classified_chunk_taints():
    """The Bell-LaPadula latch keys off this property. If a refactor
    splits chunks across multiple lists or moves the flag, the latch
    silently breaks and encrypted material leaks into unclassified
    threads via memory recall.
    """
    safe = RetrievedChunk(
        id=1, conversation_id=1, role="user", content="x",
        cosine=0.9, is_encrypted=False,
        classification_level=Classification.UNCLASSIFIED,
        classification_source="test",
    )
    classified = RetrievedChunk(
        id=2, conversation_id=2, role="assistant", content="y",
        cosine=0.8, is_encrypted=True,
        classification_level=Classification.TOP_SECRET,
        classification_source="test",
    )
    res = MemoryReadResult(
        block=None,
        facts_count=0,
        chunks=[safe, classified],
        inherited_classification=Classification.TOP_SECRET,
    )
    assert res.encryption_inherited is True


# ── MemoryAdapter Protocol contract ──────────────────────────────────────────


def test_memory_adapter_is_runtime_checkable_protocol():
    """A class that implements every async method satisfies the
    Protocol. Pin runtime_checkable behaviour so a future refactor
    that drops it doesn't silently break adapter validation.
    """

    class _Stub:
        async def get_user_facts(self, user_id):  # noqa: ARG002
            return []

        async def upsert_user_facts(self, user_id, facts, **kwargs):  # noqa: ARG002
            return None

        async def delete_user_fact(self, user_id, fact_id):  # noqa: ARG002
            return False

        async def clear_user_facts(self, user_id):  # noqa: ARG002
            return 0

        async def write_chunk(self, **kwargs):  # noqa: ARG002
            return None

        async def retrieve_relevant_chunks(self, user_id, query_text, **kwargs):  # noqa: ARG002
            return []

        async def clear_user_chunks(self, user_id):  # noqa: ARG002
            return 0

        async def build_memory_block(self, user_id, latest_user_message, **kwargs):  # noqa: ARG002
            return MemoryReadResult(block=None, facts_count=0)

        async def persist_turn(self, **kwargs):  # noqa: ARG002
            return None

    assert isinstance(_Stub(), MemoryAdapter)


# ── DTO smoke ────────────────────────────────────────────────────────────────


def test_user_fact_dto_is_immutable():
    fact = UserFactDTO(
        user_id=1,
        key="姓名",
        value="X",
        classification_level=Classification.UNCLASSIFIED,
        classification_source="test",
    )
    with pytest.raises(AttributeError):
        fact.value = "Y"  # type: ignore[misc]


def test_default_embed_model_is_documented_default():
    """Both sides of the cutover (CSP storage backend, future
    agent-side consumer) read this as the fallback. Keep it pinned
    so a deployment change doesn't accidentally reach into
    unrelated code paths.
    """
    assert DEFAULT_EMBED_MODEL == "nvidia/NV-embed-V2"
