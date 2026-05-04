"""Tests for the framework-level personalization Protocol.

Stays purely on the abstract layer — no HTTP / DB / vendor
specifics. Concrete provider implementations (in user code or
deployment-specific glue) bring their own tests.

Covers:
* :class:`UserContextProvider` Protocol acceptance via
  ``isinstance`` (so user code can validate their own
  implementation).
* :class:`NoopUserContextProvider` returns ``[]`` regardless of
  request shape.
* :func:`format_user_facts_block` rendering rules — empty input
  returns ``None`` so the chat handler can short-circuit.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agentic_rag.runtime.personalization import (
    NoopUserContextProvider,
    UserContextProvider,
    UserFact,
    format_user_facts_block,
)


# ── UserFact DTO ──────────────────────────────────────────────────────────────


def test_user_fact_is_immutable():
    fact = UserFact(key="role", value="engineer")
    with pytest.raises(AttributeError):
        fact.value = "manager"  # type: ignore[misc]


def test_user_fact_default_confidence_is_one():
    """Pin the default so callers writing ``UserFact(key=..., value=...)``
    don't have to think about confidence unless they care."""
    assert UserFact(key="x", value="y").confidence == 1.0


# ── Protocol structural typing ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_provider_satisfies_user_context_protocol():
    """``isinstance`` check must pass — runtime_checkable Protocol
    so user code can assert their own provider implements it."""
    assert isinstance(NoopUserContextProvider(), UserContextProvider)


@pytest.mark.asyncio
async def test_noop_provider_returns_empty_list_regardless_of_request():
    """Noop is the documented degradation path: no facts, no
    enrichment, chat continues as if the dependency wasn't there."""
    fake_request = MagicMock()
    facts = await NoopUserContextProvider().get_user_facts(fake_request)
    assert facts == []


@pytest.mark.asyncio
async def test_arbitrary_class_with_correct_method_satisfies_protocol():
    """Any class with the right async method passes — that's the
    Protocol contract. Validates that user code without inheriting
    from anything works.
    """

    class _CustomProvider:
        async def get_user_facts(self, request):  # noqa: ARG002
            return [UserFact(key="from", value="custom")]

    instance = _CustomProvider()
    assert isinstance(instance, UserContextProvider)
    facts = await instance.get_user_facts(MagicMock())
    assert facts == [UserFact(key="from", value="custom")]


# ── format_user_facts_block ──────────────────────────────────────────────────


def test_format_user_facts_block_returns_none_for_empty():
    """Empty list → None lets the chat handler do
    ``return enriched or base_prompt`` without an extra branch."""
    assert format_user_facts_block([]) is None


def test_format_user_facts_block_renders_markdown_with_guidance_footer():
    facts = [
        UserFact(key="name", value="Sara"),
        UserFact(key="role", value="engineer"),
    ]
    block = format_user_facts_block(facts)
    assert block is not None
    assert "## 使用者背景" in block
    assert "**name**: Sara" in block
    assert "**role**: engineer" in block
    # Guidance footer instructing the model to prefer current turn.
    assert "矛盾" in block and "本次對話為準" in block


def test_format_user_facts_block_handles_single_fact():
    """Single-fact input still gets the same block envelope so
    downstream LLM behaviour is consistent across fact-count."""
    block = format_user_facts_block([UserFact(key="lang", value="zh-TW")])
    assert block is not None
    assert "**lang**: zh-TW" in block
