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
from typing import Any, ClassVar

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


@register_chunker
class PdfPageChunker(ChunkerStrategy):
    """One chunk per PDF page (split on ``\\f`` page-marker).

    The PDF parser inserts a form-feed (``\\f``) between pages.
    This chunker splits there, then within each page falls back to
    fixed-size windowing if any single page exceeds ``max_page_tokens``.
    Each chunk's metadata records the 1-based page number so the dev
    UI / retrieval layer can surface "see page 4 of doc.pdf" cleanly.

    When the input has no ``\\f`` markers (e.g. single-page PDF, or a
    non-PDF source mistakenly routed here), behaves as the fixed
    chunker over the whole document.
    """

    name = "pdf-page"
    display_name = "PDF page boundaries"
    default_params = {"max_page_tokens": 4096}
    param_schema = {
        "type": "object",
        "properties": {
            "max_page_tokens": {"type": "integer", "minimum": 256, "maximum": 16384},
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
        max_tok = int(merged["max_page_tokens"])

        pages = [p for p in document_text.split("\f") if p.strip()]
        if not pages:
            return []

        chunks: list[ChunkResult] = []
        for page_idx, page_text in enumerate(pages, start=1):
            page_text = page_text.strip()
            base_meta = {
                "page": page_idx,
                "total_pages": len(pages),
                "strategy": self.name,
            }

            if _tokens(page_text) <= max_tok:
                chunks.append(
                    ChunkResult(
                        content=page_text,
                        chunk_key=f"page-{page_idx:04d}",
                        token_count=_tokens(page_text),
                        metadata=base_meta,
                    )
                )
                continue

            # Page exceeds budget — fall back to fixed within the page,
            # tagging each sub-chunk with the same page number so
            # retrieval still surfaces "page 4" no matter which slice hit.
            sub_chunks = FixedChunker().chunk(
                page_text, {}, {"size": max_tok, "overlap": max_tok // 16}
            )
            for sub_idx, sub in enumerate(sub_chunks):
                chunks.append(
                    ChunkResult(
                        content=sub.content,
                        chunk_key=f"page-{page_idx:04d}-{sub_idx:03d}",
                        token_count=sub.token_count,
                        metadata={**base_meta, "sub_index": sub_idx},
                    )
                )
        return chunks


# CJK sentence boundary characters. Includes ASCII fallback so mixed
# zh-en text (e.g. "結論：The result is X. 但是要注意…") still splits
# at natural sentence boundaries even on the English clauses.
_CJK_SENTENCE_RE = re.compile(r"(?<=[。！？!?\.\?])\s*", re.UNICODE)


@register_chunker
class CjkSentenceChunker(ChunkerStrategy):
    """CJK sentence-aware chunking with token-budget merging.

    Splits on Chinese sentence terminators (``。``, ``！``, ``？``) plus
    ASCII (``.``, ``!``, ``?``) so mixed-language text behaves sanely.
    Adjacent sentences are merged greedily until the running token
    count would exceed ``target_tokens`` — produces chunks closer to
    target size than naive fixed-window without breaking mid-sentence.

    Better than ``fixed`` for legal / regulatory CJK corpora where
    sentence boundaries carry semantic weight (e.g. "第八條" article
    boundaries) and arbitrary char-window splits hurt retrieval.
    """

    name = "cjk-sentence"
    display_name = "CJK sentence-aware (target-tokens merge)"
    default_params = {"target_tokens": 512, "max_tokens": 1024}
    param_schema = {
        "type": "object",
        "properties": {
            "target_tokens": {"type": "integer", "minimum": 64, "maximum": 4096},
            "max_tokens": {"type": "integer", "minimum": 64, "maximum": 8192},
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
        target = int(merged["target_tokens"])
        ceiling = int(merged["max_tokens"])
        if ceiling < target:
            raise ValueError(
                f"cjk-sentence: max_tokens ({ceiling}) must be >= target_tokens ({target})"
            )

        # Sentence split. Keep terminators by using lookbehind so
        # "第八條。第九條…" yields ["第八條。", "第九條…"] not lossy splits.
        sentences = [s for s in _CJK_SENTENCE_RE.split(document_text) if s.strip()]
        if not sentences:
            return []

        chunks: list[ChunkResult] = []
        buffer: list[str] = []
        buffer_tokens = 0
        idx = 0

        for sent in sentences:
            sent_tok = _tokens(sent)
            # Single sentence over the ceiling: emit current buffer,
            # then split this sentence with fixed-size as fallback.
            if sent_tok > ceiling:
                if buffer:
                    chunks.append(self._emit(idx, "".join(buffer), buffer_tokens))
                    idx += 1
                    buffer = []
                    buffer_tokens = 0
                long = FixedChunker().chunk(
                    sent, {}, {"size": target, "overlap": target // 8}
                )
                for sub in long:
                    chunks.append(
                        ChunkResult(
                            content=sub.content,
                            chunk_key=f"sent-{idx:04d}-long",
                            token_count=sub.token_count,
                            metadata={"strategy": self.name, "long_sentence": True},
                        )
                    )
                    idx += 1
                continue

            # Would exceed target after appending → flush first.
            if buffer and buffer_tokens + sent_tok > target:
                chunks.append(self._emit(idx, "".join(buffer), buffer_tokens))
                idx += 1
                buffer = []
                buffer_tokens = 0

            buffer.append(sent + (" " if not sent.endswith(("。", "？", "！", ".", "?", "!")) else ""))
            buffer_tokens += sent_tok

        if buffer:
            chunks.append(self._emit(idx, "".join(buffer), buffer_tokens))
        return chunks

    def _emit(self, idx: int, content: str, tokens: int) -> ChunkResult:
        return ChunkResult(
            content=content.strip(),
            chunk_key=f"sent-{idx:04d}",
            token_count=tokens,
            metadata={"strategy": self.name},
        )


@register_chunker
class SemanticChunker(ChunkerStrategy):
    """Embedding-distance-based chunking.

    Splits the document into candidate "sentence-or-paragraph" segments
    (~``min_segment_tokens`` each), then groups consecutive segments
    into chunks by detecting points where the cosine distance between
    adjacent segment embeddings exceeds the
    ``breakpoint_percentile`` threshold (i.e. semantic shift detected).
    Useful for narrative / argumentative text where heading hierarchy
    is shallow but topic shifts inside a section.

    Unlike the other built-ins, this strategy needs *embeddings* to
    work. The pure-sync ``chunk()`` interface is preserved by having
    the worker pre-compute embeddings for the candidate segments and
    stuff them into ``params["_embeddings"]``. ``requires_embedder=True``
    tells the worker to do that pre-compute pass.

    If ``_embeddings`` is missing from params (e.g. someone calls this
    directly without going through the worker) we raise a clear
    ``ValueError`` with the contract reminder.
    """

    name = "semantic"
    display_name = "Semantic boundaries (embedding distance)"
    requires_embedder: ClassVar[bool] = True
    default_params = {
        "min_segment_tokens": 128,
        "breakpoint_percentile": 80,
    }
    param_schema = {
        "type": "object",
        "properties": {
            "min_segment_tokens": {"type": "integer", "minimum": 32, "maximum": 1024},
            "breakpoint_percentile": {"type": "integer", "minimum": 50, "maximum": 99},
        },
        "additionalProperties": False,
    }

    @staticmethod
    def split_segments(text: str, min_tokens: int) -> list[str]:
        """Public helper: tokenise text into candidate segments.

        Worker calls this first, embeds the result, then passes it back
        in via ``params["_embeddings"]`` and ``params["_segments"]``.
        Exposed as a static method so the worker can stay decoupled
        from chunk() internals.
        """
        # Sentence-level first; merge tiny ones up to min_tokens.
        sents = [s for s in _CJK_SENTENCE_RE.split(text) if s.strip()]
        if not sents:
            return []
        out: list[str] = []
        buffer: list[str] = []
        buf_tok = 0
        for s in sents:
            buffer.append(s)
            buf_tok += _tokens(s)
            if buf_tok >= min_tokens:
                out.append("".join(buffer))
                buffer = []
                buf_tok = 0
        if buffer:
            (out.append("".join(buffer)) if not out else
             # Tail merges into the previous segment when below min.
             out.__setitem__(-1, out[-1] + "".join(buffer)))
        return out

    def chunk(
        self,
        document_text: str,
        metadata: dict[str, Any],
        params: dict[str, Any],
    ) -> list[ChunkResult]:
        merged = {**self.default_params, **params}
        breakpoint_pct = int(merged["breakpoint_percentile"])

        segments: list[str] | None = params.get("_segments")
        embeddings: list[list[float]] | None = params.get("_embeddings")

        if segments is None or embeddings is None:
            raise ValueError(
                "SemanticChunker requires the worker to pre-compute "
                "segments + embeddings and pass them via "
                "params['_segments'] and params['_embeddings']. "
                "Call SemanticChunker.split_segments() first then "
                "embed each segment."
            )
        if len(segments) != len(embeddings):
            raise ValueError(
                f"semantic: segment / embedding count mismatch "
                f"({len(segments)} vs {len(embeddings)})"
            )
        if not segments:
            return []
        if len(segments) == 1:
            return [
                ChunkResult(
                    content=segments[0],
                    chunk_key="seg-0000",
                    token_count=_tokens(segments[0]),
                    metadata={"strategy": self.name, "segments": 1},
                )
            ]

        # Cosine distance between adjacent segments.
        distances: list[float] = []
        for a, b in zip(embeddings, embeddings[1:]):
            distances.append(_cosine_distance(a, b))

        # Threshold = the ``breakpoint_pct``th percentile of distances.
        # Above threshold = semantic shift = new chunk boundary.
        threshold = _percentile(distances, breakpoint_pct)
        boundaries = [0]
        for i, d in enumerate(distances, start=1):
            if d >= threshold:
                boundaries.append(i)
        boundaries.append(len(segments))

        chunks: list[ChunkResult] = []
        for idx, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
            content = "".join(segments[start:end]).strip()
            if not content:
                continue
            chunks.append(
                ChunkResult(
                    content=content,
                    chunk_key=f"seg-{idx:04d}",
                    token_count=_tokens(content),
                    metadata={
                        "strategy": self.name,
                        "segment_range": [start, end],
                        "boundary_distance_threshold": round(threshold, 4),
                    },
                )
            )
        return chunks


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity. Both vectors assumed non-zero."""
    if len(a) != len(b):
        raise ValueError(f"cosine: dim mismatch {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - (dot / (norm_a * norm_b))


def _percentile(xs: list[float], pct: int) -> float:
    """Numpy-free percentile. ``pct`` is 0..100; clamps at boundaries."""
    if not xs:
        return 0.0
    sorted_xs = sorted(xs)
    k = (len(sorted_xs) - 1) * pct / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = k - lo
    return sorted_xs[lo] * (1 - frac) + sorted_xs[hi] * frac
