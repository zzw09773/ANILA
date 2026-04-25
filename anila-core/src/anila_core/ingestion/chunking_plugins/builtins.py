"""Built-in chunking strategies for Sprint 1.

Three strategies cover the broadest use cases without pulling heavy
dependencies (no tiktoken, no transformers, no docling) — that lives in
the ingestion-worker service. anila-core ships only the algorithm so
SDK consumers can preview chunks locally.

- ``hierarchical``    — splits on Markdown-style heading hierarchy, then
                        falls back to token-budget within each leaf.
                        Default for prose-heavy corpora.
- ``fixed``           — naive token-budget windowing with overlap. Cheap
                        baseline; useful when documents have no structure.
- ``markdown-aware``  — heading-respecting AND code-fence-respecting; never
                        splits inside a fenced code block.

The remaining three strategies (``pdf-page`` / ``cjk-sentence`` /
``semantic``) ship in Sprint 2 and 3 and live in the worker service
because they need PDF / tokenizer / embedding deps.

Token approximation: we use ``len(text) // 4`` as a stand-in for tiktoken.
Acceptable because chunkers are deterministic given inputs — actual
embedding-time token count is what matters for similarity, and the
``token_count`` field on ``ChunkResult`` is advisory (used for UI sort,
not retrieval).
"""

from __future__ import annotations

import re
from typing import Any

from anila_core.ingestion.chunking_plugins.base import ChunkResult, ChunkerStrategy
from anila_core.ingestion.chunking_plugins.registry import register_chunker

# 4 chars-per-token is the same heuristic OpenAI's "rough estimate" page uses.
# Good enough for chunk-sizing within ±25%; not used for embedding billing.
_CHARS_PER_TOKEN = 4

# Markdown ATX heading pattern: 1-6 leading hashes, then space, then title.
# Captured groups: (1) hash count = level, (2) title text. Multiline-anchored.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", flags=re.MULTILINE)

# Fenced code block pattern (``` or ~~~). DOTALL so ``.+?`` spans newlines.
_CODE_FENCE_RE = re.compile(r"^(```|~~~).*?^\1\s*$", flags=re.MULTILINE | re.DOTALL)


def _tokens(text: str) -> int:
    """Rough token count without pulling tiktoken into core."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _slug(s: str, maxlen: int = 40) -> str:
    """Filesystem-/URL-safe id fragment for chunk_key."""
    cleaned = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", s.strip()).strip("-")
    return cleaned[:maxlen] or "section"


@register_chunker
class FixedChunker(ChunkerStrategy):
    """Token-budget windowing with overlap — no structural awareness."""

    name = "fixed"
    display_name = "Fixed-size (token-budget)"
    default_params = {"size": 1024, "overlap": 128}
    param_schema = {
        "type": "object",
        "properties": {
            "size": {"type": "integer", "minimum": 64, "maximum": 8192},
            "overlap": {"type": "integer", "minimum": 0, "maximum": 1024},
        },
        "required": ["size"],
        "additionalProperties": False,
    }

    def chunk(
        self,
        document_text: str,
        metadata: dict[str, Any],
        params: dict[str, Any],
    ) -> list[ChunkResult]:
        merged = {**self.default_params, **params}
        size_tok = int(merged["size"])
        overlap_tok = int(merged["overlap"])
        if overlap_tok >= size_tok:
            # Guard: overlap >= size would cause infinite loop / zero-progress.
            raise ValueError(
                f"fixed: overlap ({overlap_tok}) must be < size ({size_tok})"
            )

        size_chars = size_tok * _CHARS_PER_TOKEN
        step_chars = (size_tok - overlap_tok) * _CHARS_PER_TOKEN
        chunks: list[ChunkResult] = []
        text_len = len(document_text)
        idx = 0
        cursor = 0
        while cursor < text_len:
            piece = document_text[cursor : cursor + size_chars]
            if not piece.strip():
                cursor += step_chars
                continue
            chunks.append(
                ChunkResult(
                    content=piece,
                    chunk_key=f"chunk-{idx:05d}",
                    token_count=_tokens(piece),
                    metadata={"offset_chars": cursor, "strategy": self.name},
                )
            )
            idx += 1
            cursor += step_chars
        return chunks


@register_chunker
class MarkdownAwareChunker(ChunkerStrategy):
    """Splits on headings; never splits inside a fenced code block."""

    name = "markdown-aware"
    display_name = "Markdown-aware (headings + code-fence safe)"
    default_params = {"max_leaf_tokens": 1024}
    param_schema = {
        "type": "object",
        "properties": {
            "max_leaf_tokens": {"type": "integer", "minimum": 128, "maximum": 8192},
        },
        "additionalProperties": False,
    }

    def chunk(
        self,
        document_text: str,
        metadata: dict[str, Any],
        params: dict[str, Any],
    ) -> list[ChunkResult]:
        merged = {**self.default_params, **params}
        max_tok = int(merged["max_leaf_tokens"])

        # Mask code fences so heading regex inside fences is ignored.
        # We replace fence content with same-length spaces — preserves
        # absolute offsets for header detection.
        masked = _CODE_FENCE_RE.sub(lambda m: " " * len(m.group(0)), document_text)

        # Find heading boundaries (start position of each heading).
        heading_starts = [m.start() for m in _HEADING_RE.finditer(masked)]
        # Treat document start as a section boundary too.
        boundaries = [0, *heading_starts, len(document_text)]

        chunks: list[ChunkResult] = []
        idx = 0
        for start, end in zip(boundaries, boundaries[1:]):
            section = document_text[start:end]
            if not section.strip():
                continue

            heading_match = _HEADING_RE.match(section)
            heading_title = (
                heading_match.group(2).strip() if heading_match else "preface"
            )
            heading_level = len(heading_match.group(1)) if heading_match else 0

            # If the section fits the budget, emit one chunk per heading.
            # We *don't* merge short sections into the previous chunk:
            # each authored heading is a deliberate boundary and the dev
            # likely wants distinct embedding vectors per section.
            if _tokens(section) <= max_tok:
                chunks.append(
                    ChunkResult(
                        content=section,
                        chunk_key=f"sec-{idx:04d}-{_slug(heading_title)}",
                        token_count=_tokens(section),
                        metadata={
                            "heading": heading_title,
                            "heading_level": heading_level,
                            "strategy": self.name,
                        },
                    )
                )
                idx += 1
                continue

            # Section exceeds budget: fall back to fixed-size window
            # within this section, preserving the heading metadata.
            sub_chunks = FixedChunker().chunk(
                section, {}, {"size": max_tok, "overlap": max_tok // 16}
            )
            for sub_idx, sub in enumerate(sub_chunks):
                chunks.append(
                    ChunkResult(
                        content=sub.content,
                        chunk_key=(
                            f"sec-{idx:04d}-{_slug(heading_title)}-{sub_idx:03d}"
                        ),
                        token_count=sub.token_count,
                        metadata={
                            "heading": heading_title,
                            "heading_level": heading_level,
                            "sub_index": sub_idx,
                            "strategy": self.name,
                        },
                    )
                )
            idx += 1
        return chunks


@register_chunker
class HierarchicalChunker(ChunkerStrategy):
    """Multi-level heading tree with parent-context preserved per leaf.

    Differs from ``markdown-aware`` by retaining the FULL ancestor chain
    in each leaf's metadata (``heading_path: ["Chapter 1", "Section 1.2",
    "Subsection 1.2.3"]``). Retrieval then surfaces the parent path back
    into prompts so the LLM sees structural context — important for legal
    / regulation corpora where "第八條" alone is meaningless without
    knowing which Chapter / Article-set it belongs to.
    """

    name = "hierarchical"
    display_name = "Hierarchical (heading tree + ancestor context)"
    default_params = {"max_leaf_tokens": 1024, "overlap_tokens": 64}
    param_schema = {
        "type": "object",
        "properties": {
            "max_leaf_tokens": {"type": "integer", "minimum": 128, "maximum": 8192},
            "overlap_tokens": {"type": "integer", "minimum": 0, "maximum": 512},
        },
        "additionalProperties": False,
    }

    def chunk(
        self,
        document_text: str,
        metadata: dict[str, Any],
        params: dict[str, Any],
    ) -> list[ChunkResult]:
        merged = {**self.default_params, **params}
        max_tok = int(merged["max_leaf_tokens"])
        overlap_tok = int(merged["overlap_tokens"])

        masked = _CODE_FENCE_RE.sub(lambda m: " " * len(m.group(0)), document_text)
        # Maintain a heading stack indexed by level (1..6).
        # `heading_stack[i]` is the most-recent heading at level i (or None).
        heading_stack: list[str | None] = [None] * 7
        # Collect (start_offset, end_offset, heading_path) for each leaf.
        leaves: list[tuple[int, int, list[str]]] = []
        last_pos = 0

        for m in _HEADING_RE.finditer(masked):
            start = m.start()
            level = len(m.group(1))
            title = m.group(2).strip()

            # Close out the leaf preceding this heading.
            if start > last_pos:
                leaves.append((last_pos, start, self._snapshot(heading_stack)))

            # Update stack: new heading at `level` clears all deeper levels.
            heading_stack[level] = title
            for deeper in range(level + 1, 7):
                heading_stack[deeper] = None

            last_pos = start

        # Final leaf from the last heading to EOF.
        if last_pos < len(document_text):
            leaves.append(
                (last_pos, len(document_text), self._snapshot(heading_stack))
            )

        chunks: list[ChunkResult] = []
        idx = 0
        for start, end, path in leaves:
            section = document_text[start:end]
            if not section.strip():
                continue

            if _tokens(section) <= max_tok:
                chunks.append(self._make_chunk(idx, section, path))
                idx += 1
                continue

            # Leaf too big — recurse into fixed-size with overlap, keeping
            # the parent heading path on every sub-chunk.
            sub_chunks = FixedChunker().chunk(
                section, {}, {"size": max_tok, "overlap": overlap_tok}
            )
            for sub_idx, sub in enumerate(sub_chunks):
                chunks.append(self._make_chunk(idx, sub.content, path, sub_idx))
                idx += 1
        return chunks

    @staticmethod
    def _snapshot(stack: list[str | None]) -> list[str]:
        """Return current heading_path with Nones stripped."""
        return [h for h in stack if h is not None]

    def _make_chunk(
        self,
        idx: int,
        content: str,
        heading_path: list[str],
        sub_idx: int | None = None,
    ) -> ChunkResult:
        slug_tail = _slug(heading_path[-1]) if heading_path else "preface"
        chunk_key = (
            f"leaf-{idx:04d}-{slug_tail}"
            if sub_idx is None
            else f"leaf-{idx:04d}-{slug_tail}-{sub_idx:03d}"
        )
        return ChunkResult(
            content=content,
            chunk_key=chunk_key,
            token_count=_tokens(content),
            metadata={
                "heading_path": heading_path,
                "heading_depth": len(heading_path),
                "strategy": self.name,
            },
        )
