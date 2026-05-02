"""Tests for B3 — relevance selector model param + memory injection helper."""

from __future__ import annotations

import pytest

from agentic_rag.memory.relevance_selector import (
    MEMORY_INJECTION_HEADER,
    ModelBasedRelevanceSelector,
    RelevantMemory,
    render_memories_for_prompt,
)
from agentic_rag.models.memory import MemoryHeader, MemoryType
from agentic_rag.models.message import StreamDelta, Usage


# ── Constructor model param ───────────────────────────────────────────


def test_relevance_selector_requires_model() -> None:
    class FakeProvider:
        async def stream_completion(self, request):
            yield

    with pytest.raises(ValueError, match="explicit model"):
        ModelBasedRelevanceSelector(FakeProvider(), model="")


def test_relevance_selector_accepts_model() -> None:
    class FakeProvider:
        async def stream_completion(self, request):
            yield

    sel = ModelBasedRelevanceSelector(FakeProvider(), model="haiku-local")
    assert sel._model == "haiku-local"


# ── Side query routes the configured model to the provider ────────────


@pytest.mark.asyncio
async def test_side_query_passes_configured_model_to_provider() -> None:
    seen = {}

    class CapturingProvider:
        async def stream_completion(self, request):
            seen["model"] = request.model
            yield StreamDelta(
                type="text",
                text='{"selected_memories": ["a.md"]}',
            )
            yield StreamDelta(type="stop", finish_reason="stop", usage=Usage())

    selector = ModelBasedRelevanceSelector(CapturingProvider(), model="haiku-local")

    headers = [
        MemoryHeader(
            filename="a.md",
            file_path="/mem/a.md",
            title="A",
            description="thing about a",
            memory_type=MemoryType.GENERAL,
        )
    ]

    result = await selector.select(
        query="any", memory_headers=headers, recent_tools=[], already_surfaced=set()
    )
    assert seen["model"] == "haiku-local"
    assert len(result) == 1
    assert result[0].filename == "a.md"


# ── render_memories_for_prompt ────────────────────────────────────────


def _mem(name: str) -> RelevantMemory:
    return RelevantMemory(path=f"/mem/{name}", mtime_ms=0.0, filename=name)


def test_render_empty_list_returns_empty_string() -> None:
    assert render_memories_for_prompt([]) == ""


def test_render_includes_header_and_each_memory_body() -> None:
    bodies = {
        "/mem/user.md": "User prefers concise answers.",
        "/mem/project.md": "Project uses pytest.",
    }

    def fake_read(path: str) -> str:
        return bodies[path]

    out = render_memories_for_prompt(
        [_mem("user.md"), _mem("project.md")],
        read_body=fake_read,
    )
    assert MEMORY_INJECTION_HEADER in out
    assert "### user.md" in out
    assert "User prefers concise answers." in out
    assert "### project.md" in out
    assert "Project uses pytest." in out


def test_render_truncates_oversized_memory_body() -> None:
    long_body = "x" * 5_000

    def fake_read(_path: str) -> str:
        return long_body

    out = render_memories_for_prompt(
        [_mem("big.md")], read_body=fake_read, max_chars_per_memory=100
    )
    assert "[…truncated…]" in out
    # Body chunk + truncation marker; total length contribution well below 5000.
    assert len(out) < 1_000


def test_render_skips_memory_when_read_fails() -> None:
    def boom(_path: str) -> str:
        raise FileNotFoundError("gone")

    # Mix of failing and succeeding reads
    def maybe(path: str) -> str:
        if path.endswith("ok.md"):
            return "kept"
        raise FileNotFoundError("gone")

    out = render_memories_for_prompt(
        [_mem("missing.md"), _mem("ok.md")], read_body=maybe
    )
    assert "missing.md" not in out
    assert "kept" in out


def test_render_returns_empty_when_all_reads_fail() -> None:
    def boom(_path: str) -> str:
        raise OSError("nope")

    out = render_memories_for_prompt([_mem("a.md"), _mem("b.md")], read_body=boom)
    assert out == ""


def test_render_skips_empty_body() -> None:
    out = render_memories_for_prompt([_mem("a.md")], read_body=lambda _p: "")
    assert out == ""
