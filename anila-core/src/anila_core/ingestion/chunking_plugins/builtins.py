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

# Latin-script token density. OpenAI's CL100K BPE averages ~4 chars per
# token for English / Latin-script content. Used as the divisor for
# the non-CJK portion of any text.
_CHARS_PER_TOKEN = 4

# Markdown ATX heading pattern: 1-6 leading hashes, then space, then title.
# Captured groups: (1) hash count = level, (2) title text. Multiline-anchored.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", flags=re.MULTILINE)

# Fenced code block pattern (``` or ~~~). DOTALL so ``.+?`` spans newlines.
_CODE_FENCE_RE = re.compile(r"^(```|~~~).*?^\1\s*$", flags=re.MULTILINE | re.DOTALL)


def _is_cjk(c: str) -> bool:
    """Return True for CJK ideograph / kana / hangul characters.

    Covers CJK Unified Ideographs, Hiragana, Katakana, Hangul Syllables.
    Used to switch the chars-per-token heuristic — BPE collapses
    Latin-script text into ~4 chars/token but Asian scripts stay
    closer to 1 char/token because each ideograph is its own concept.
    """
    return (
        "一" <= c <= "鿿"   # CJK Unified Ideographs
        or "぀" <= c <= "ヿ"  # Hiragana + Katakana
        or "가" <= c <= "힯"  # Hangul Syllables
    )


def _tokens(text: str) -> int:
    """Approximate token count without pulling tiktoken into core.

    Mixed-script aware: CJK characters count ~1 token each, Latin
    text ~``_CHARS_PER_TOKEN`` chars per token. The previous flat
    ``len // 4`` was English-biased and undercounted CJK by roughly
    4×, which caused fixed-size chunkers to produce chunks far
    larger than the user's stated ``size`` budget for Chinese /
    Japanese / Korean docs.

    Still a heuristic — real tokenisation (tiktoken / sentencepiece)
    is the ground truth — but acceptable for chunk sizing where ±10%
    is fine and we'd rather not pull a heavyweight dep into core.
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if _is_cjk(c))
    other = len(text) - cjk
    return max(1, cjk + other // _CHARS_PER_TOKEN)


def _estimate_char_budget(text: str, target_tokens: int) -> int:
    """Inverse of ``_tokens`` — how many characters approximate
    ``target_tokens`` for **this specific text**'s language mix.

    Sliding-window chunkers need to step in CHARS but the user's
    parameter is in TOKENS. Scaling by a flat constant fails for
    mixed / CJK content. We sample the head of the document, compute
    an empirical chars-per-token, and return the budget. Cheap
    (one O(n) sample pass) but adapts to content.
    """
    if not text or target_tokens <= 0:
        return 0
    # Sample up to 2048 chars from the document head. For docs larger
    # than that the language mix in the head is a fine proxy for the
    # whole; smaller docs use the entire text.
    sample = text[: min(2048, len(text))]
    sample_tokens = _tokens(sample)
    if sample_tokens <= 0:
        return target_tokens * _CHARS_PER_TOKEN
    chars_per_token = len(sample) / sample_tokens
    return max(1, int(target_tokens * chars_per_token))


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

        # CJK-aware sizing — see ``_estimate_char_budget``. The previous
        # ``size_tok * 4`` constant overshot Chinese / Japanese / Korean
        # text by ~4× because BPE behaves differently on those scripts.
        size_chars = _estimate_char_budget(document_text, size_tok)
        step_chars = _estimate_char_budget(document_text, size_tok - overlap_tok)
        if step_chars <= 0:
            # Defensive — shouldn't happen given the overlap < size guard
            # above plus min-1 inside the helper, but cheap insurance
            # against an infinite loop on pathological inputs.
            step_chars = 1
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
    """Multi-level heading tree — emits parent (heading) + leaf rows.

    Sprint 9 X / parent-child RAG redesign. Now produces a TREE of
    chunks instead of a flat list of leaves:

    1. One ``chunk_type='heading'`` row per heading the chunker
       discovers. ``content`` is the heading title; embedding stays
       NULL (heading rows are JOIN-fetched, never vector-searched).
    2. One or more ``chunk_type='leaf'`` rows under each heading.
       Each carries ``parent_chunk_key`` in metadata so the worker
       can resolve the FK after the parent row is inserted.

    A "leaf" is a paragraph-sized chunk (~``max_leaf_tokens``,
    default 256 — was 1024 pre-9-X). Sections that exceed the leaf
    budget recurse into fixed-size windowing inside the section,
    keeping the heading path attached.

    Result: small chunks for vector recall + structural parent for
    LLM context. The worker's persistence layer is responsible for
    writing parents before children so the FK can be filled in.
    Until that wiring lands (Phase 3), parent_chunk_key is metadata-
    only and the rows still flow through pgvector_store as
    independent chunks.
    """

    name = "hierarchical"
    display_name = "Hierarchical (heading tree + ancestor context)"
    default_params = {
        # Sprint 9 X: dropped from 1024 → 256 (paragraph-level leaves)
        # to give vector retrieval finer granularity. Use the
        # ``max_parent_tokens`` budget for the heading-section
        # context that gets JOIN-fetched at retrieval.
        "max_leaf_tokens": 256,
        "max_parent_tokens": 1024,
        "overlap_tokens": 32,
    }
    param_schema = {
        "type": "object",
        "properties": {
            "max_leaf_tokens": {"type": "integer", "minimum": 64, "maximum": 4096},
            "max_parent_tokens": {"type": "integer", "minimum": 128, "maximum": 8192},
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
        max_leaf = int(merged["max_leaf_tokens"])
        max_parent = int(merged["max_parent_tokens"])
        overlap_tok = int(merged["overlap_tokens"])

        masked = _CODE_FENCE_RE.sub(lambda m: " " * len(m.group(0)), document_text)

        # Walk the heading regex once, building a list of section
        # records: (heading_title, heading_level, body_start,
        # body_end, heading_path_at_this_section).
        sections = self._extract_sections(document_text, masked)

        chunks: list[ChunkResult] = []
        # Track which heading (chunk_key) is the current parent for
        # subsequent leaf emission. Sections without their own
        # heading (i.e. preamble before the first ``#``) attach to
        # an implicit ``preface`` parent so leaves still have a
        # parent_chunk_key.
        heading_idx = 0
        leaf_idx = 0

        for section in sections:
            title = section["title"]
            level = section["level"]
            body = section["body"]
            heading_path = section["path"]

            # Emit the heading-level parent row (no embedding).
            # ``content`` is the heading title — what the JOIN at
            # retrieval time will return as ``parent_content``.
            parent_chunk_key = (
                f"heading-{heading_idx:04d}-{_slug(title) or 'preface'}"
            )
            chunks.append(
                ChunkResult(
                    content=title or "(preface)",
                    chunk_key=parent_chunk_key,
                    token_count=_tokens(title or ""),
                    metadata={
                        "chunk_type": "heading",
                        "chunk_level": level,
                        "heading_path": heading_path,
                        "heading_depth": len(heading_path),
                        "strategy": self.name,
                    },
                )
            )
            heading_idx += 1

            if not body.strip():
                continue

            # Decide leaf granularity for this section's body. We
            # split into paragraph-sized leaves (~max_leaf tokens).
            # Paragraph boundaries = blank-line gaps; if a paragraph
            # itself exceeds the leaf budget, recurse into fixed-size
            # windows inside that paragraph with overlap.
            leaves = self._split_into_leaves(body, max_leaf, overlap_tok)
            for leaf_text in leaves:
                if not leaf_text.strip():
                    continue
                leaf_key = (
                    f"leaf-{leaf_idx:05d}-{_slug(title) or 'preface'}"
                )
                chunks.append(
                    ChunkResult(
                        content=leaf_text,
                        chunk_key=leaf_key,
                        token_count=_tokens(leaf_text),
                        metadata={
                            "chunk_type": "leaf",
                            "chunk_level": level + 1,
                            "heading_path": heading_path,
                            "heading_depth": len(heading_path),
                            "parent_chunk_key": parent_chunk_key,
                            "strategy": self.name,
                        },
                    )
                )
                leaf_idx += 1

        # Note: ``max_parent_tokens`` is currently informational —
        # parent rows store only the heading title, not the section
        # body, so they're always small. Reserved for a future
        # variant where parents carry a section summary or first-N-
        # tokens body for richer JOIN content.
        _ = max_parent

        return chunks

    @staticmethod
    def _extract_sections(
        document_text: str, masked: str
    ) -> list[dict[str, Any]]:
        """Walk heading regex; return one record per section.

        A "section" is the span between one heading and the next.
        Document preface (text before any ``#``) becomes a synthetic
        ``level=0`` section with empty title so it still gets a
        parent row to anchor its leaves.
        """
        records: list[dict[str, Any]] = []
        heading_stack: list[str | None] = [None] * 7

        # Snapshot before any heading: an implicit preface section.
        preface_end = len(document_text)
        first_heading = next(_HEADING_RE.finditer(masked), None)
        if first_heading is not None:
            preface_end = first_heading.start()
        if preface_end > 0:
            records.append(
                {
                    "title": "",
                    "level": 0,
                    "body": document_text[:preface_end],
                    "path": [],
                }
            )

        # Walk all headings, recording the body span between each
        # heading and the next.
        heading_positions = list(_HEADING_RE.finditer(masked))
        for i, m in enumerate(heading_positions):
            start = m.start()
            level = len(m.group(1))
            title = m.group(2).strip()

            heading_stack[level] = title
            for deeper in range(level + 1, 7):
                heading_stack[deeper] = None

            body_start = m.end()
            body_end = (
                heading_positions[i + 1].start()
                if i + 1 < len(heading_positions)
                else len(document_text)
            )
            records.append(
                {
                    "title": title,
                    "level": level,
                    "body": document_text[body_start:body_end],
                    "path": [h for h in heading_stack if h is not None],
                }
            )

        return records

    @staticmethod
    def _split_into_leaves(
        body: str, max_leaf_tokens: int, overlap_tokens: int
    ) -> list[str]:
        """Split a section body into paragraph-sized leaves.

        First pass: split on blank-line gaps (paragraph boundaries).
        Second pass: any paragraph still over budget falls back to
        FixedChunker windowing with overlap (mirrors the pre-9-X
        section-overflow behaviour).
        """
        # Greedy merge of consecutive paragraphs up to max_leaf.
        paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
        if not paragraphs:
            return []

        merged: list[str] = []
        buf: list[str] = []
        buf_tok = 0
        for p in paragraphs:
            p_tok = _tokens(p)
            if p_tok > max_leaf_tokens:
                # Flush buffer first, then recurse on the oversized
                # paragraph via fixed-size windowing.
                if buf:
                    merged.append("\n\n".join(buf))
                    buf, buf_tok = [], 0
                fixed = FixedChunker().chunk(
                    p, {}, {"size": max_leaf_tokens, "overlap": overlap_tokens}
                )
                merged.extend(c.content for c in fixed)
                continue
            if buf_tok + p_tok > max_leaf_tokens and buf:
                merged.append("\n\n".join(buf))
                buf, buf_tok = [], 0
            buf.append(p)
            buf_tok += p_tok
        if buf:
            merged.append("\n\n".join(buf))
        return merged


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
