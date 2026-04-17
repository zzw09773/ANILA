"""Tests for MockEmbeddingProvider."""

from __future__ import annotations

import pytest

from anila_core.providers.embedding_mock import MockEmbeddingProvider


@pytest.mark.asyncio
async def test_embed_returns_correct_dimension():
    provider = MockEmbeddingProvider(dimension=128)
    result = await provider.embed(["hello world"])
    assert len(result) == 1
    assert len(result[0]) == 128


@pytest.mark.asyncio
async def test_embed_batch():
    provider = MockEmbeddingProvider(dimension=64)
    result = await provider.embed(["text one", "text two", "text three"])
    assert len(result) == 3
    assert all(len(v) == 64 for v in result)


@pytest.mark.asyncio
async def test_embed_deterministic():
    provider = MockEmbeddingProvider(dimension=64)
    r1 = await provider.embed(["same text"])
    r2 = await provider.embed(["same text"])
    assert r1 == r2


@pytest.mark.asyncio
async def test_embed_different_texts_differ():
    provider = MockEmbeddingProvider(dimension=64)
    r1 = await provider.embed(["text A"])
    r2 = await provider.embed(["text B"])
    assert r1[0] != r2[0]


@pytest.mark.asyncio
async def test_embed_empty_returns_empty():
    provider = MockEmbeddingProvider()
    result = await provider.embed([])
    assert result == []


def test_dimension_property():
    provider = MockEmbeddingProvider(dimension=4096)
    assert provider.dimension == 4096
