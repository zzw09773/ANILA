"""Hierarchical chunker for the AgenticRAG pipeline.

Builds a tree of ``DocumentChunk`` nodes from a Markdown-flavoured
document produced by the parsers. The tree shape is driven entirely by
document structure — headings, sub-headings, paragraphs, and inline
image placeholders — NOT by a fixed token budget.

Tree example for a PDF:

    Document (level 0, type=DOCUMENT)
      ├─ "Chapter 1" (level 1, type=HEADING)
      │    ├─ "Section 1.1" (level 2, type=HEADING)
      │    │    ├─ paragraph text (level 3, type=CONTENT)
      │    │    └─ image caption   (level 3, type=IMAGE)
      │    └─ "Section 1.2" (level 2, type=HEADING)
      │         └─ paragraph text (level 3, type=CONTENT)
      └─ "Chapter 2" (level 1, type=HEADING)
           └─ ...

Only ``CONTENT`` and ``IMAGE`` leaves carry an embedding and participate
in vector search. Headings and the document root are stored so that
retrieval can walk up for context and citation.

Back-compat: a ``RecursiveTextSplitter`` class is kept as a thin alias
pointing at ``HierarchicalChunker`` so existing imports keep working.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from ..models.storage import ChunkType, DocumentChunk
from .parsers import ImageRef


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_IMAGE_MARKER_RE = re.compile(r"\[\[IMAGE:([A-Za-z0-9_-]+)\]\]")

# Ordered strongest-to-weakest. Both ASCII and CJK forms are kept so the
# chunker still works on text that bypassed normalize_zh().
_SUBSPLIT_SEPARATORS: tuple[str, ...] = (
    "\n\n",
    "\n",
    "。", ".",
    "！", "!",
    "？", "?",
    "；", ";",
    "，", ",",
    "、",
    " ",
)


def _default_length(text: str) -> int:
    """Rough token estimation: characters / 4."""
    return max(len(text) // 4, 1)


@dataclass
class _Node:
    """Intermediate tree node before we materialise DocumentChunk."""

    chunk_id: str
    level: int
    chunk_type: ChunkType
    title: str
    body: str = ""
    heading_path: list[str] = field(default_factory=list)
    children: list["_Node"] = field(default_factory=list)
    parent: Optional["_Node"] = None
    metadata: dict = field(default_factory=dict)


class HierarchicalChunker:
    """Split a Markdown document into a tree keyed by heading structure.

    Args:
        max_leaf_tokens: soft cap — a single paragraph larger than this
            is sub-split on sentence / whitespace boundaries so the
            embedder doesn't reject it. Set to 0 to disable sub-splitting.
        overlap_tokens:  overlap used only when a leaf is sub-split.
        length_function: text → token-estimate.
    """

    def __init__(
        self,
        max_leaf_tokens: int = 1024,
        overlap_tokens: int = 64,
        length_function: Optional[Callable[[str], int]] = None,
    ) -> None:
        self._max_leaf = max_leaf_tokens
        self._overlap = overlap_tokens
        self._length = length_function or _default_length

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chunk(
        self,
        text: str,
        metadata: Optional[dict] = None,
        document_id: Optional[str] = None,
        user_id: str = "",
        project_id: str = "",
        images: Optional[dict[str, ImageRef]] = None,
    ) -> list[DocumentChunk]:
        """Build the hierarchical chunk list for a parsed document.

        Args:
            text:        Markdown-ish body with optional ``[[IMAGE:id]]``
                         placeholders.
            metadata:    Base metadata forwarded to every chunk.
            document_id: Stable ID of the parent document.
            user_id:     Owner user ID.
            project_id:  Project scope.
            images:      Map of ``image_id → ImageRef`` (with ``caption``
                         already filled in by the VLM step). Markers
                         without a matching entry fall back to placeholder
                         text.
        """
        doc_id = document_id or str(uuid.uuid4())
        base_meta = dict(metadata or {})
        image_map = images or {}

        root_title = str(base_meta.get("title") or "Untitled")
        root = _Node(
            chunk_id=str(uuid.uuid4()),
            level=0,
            chunk_type=ChunkType.DOCUMENT,
            title=root_title,
            body="",
            heading_path=[],
            metadata={**base_meta, "document_id": doc_id},
        )

        self._build_tree(text or "", root, image_map)

        chunks: list[DocumentChunk] = []
        self._flatten(
            node=root,
            parent_id=None,
            chunks=chunks,
            doc_id=doc_id,
            user_id=user_id,
            project_id=project_id,
            base_meta=base_meta,
        )
        return chunks

    # ------------------------------------------------------------------
    # Tree construction
    # ------------------------------------------------------------------

    def _build_tree(
        self,
        text: str,
        root: _Node,
        image_map: dict[str, ImageRef],
    ) -> None:
        """Walk the markdown line-by-line and build a heading tree."""
        # Stack of open heading nodes, indexed by their level.
        # stack[0] is always the root.
        stack: list[_Node] = [root]
        buffer: list[str] = []

        def flush_buffer(parent: _Node) -> None:
            raw = "\n".join(buffer).strip()
            buffer.clear()
            if not raw:
                return
            self._emit_content_and_images(raw, parent, image_map)

        for line in text.splitlines():
            m = _HEADING_RE.match(line.rstrip())
            if m:
                # New heading — flush pending content into the current parent.
                flush_buffer(stack[-1])
                level = len(m.group(1))
                title = m.group(2).strip()

                # Pop the stack until its top's level is strictly less
                # than this heading's level (so the new heading becomes a
                # sibling of nodes at the same level).
                while stack and stack[-1].level >= level:
                    stack.pop()
                if not stack:
                    stack = [root]

                parent = stack[-1]
                heading_node = _Node(
                    chunk_id=str(uuid.uuid4()),
                    level=level,
                    chunk_type=ChunkType.HEADING,
                    title=title,
                    heading_path=[*parent.heading_path, title],
                    parent=parent,
                )
                parent.children.append(heading_node)
                stack.append(heading_node)
            else:
                buffer.append(line)

        # Flush tail content into the deepest open heading (or root).
        flush_buffer(stack[-1])

    def _emit_content_and_images(
        self,
        block: str,
        parent: _Node,
        image_map: dict[str, ImageRef],
    ) -> None:
        """Split a block by image markers into alternating CONTENT and
        IMAGE leaves and attach them to *parent*."""
        # Split while keeping the markers so we can classify each piece.
        pieces: list[tuple[str, str]] = []  # (kind, payload)
        last_end = 0
        for match in _IMAGE_MARKER_RE.finditer(block):
            if match.start() > last_end:
                pieces.append(("text", block[last_end : match.start()]))
            pieces.append(("image", match.group(1)))
            last_end = match.end()
        if last_end < len(block):
            pieces.append(("text", block[last_end:]))

        child_level = parent.level + 1

        for kind, payload in pieces:
            if kind == "text":
                # Split paragraphs on blank lines to get finer leaves.
                for para in _split_paragraphs(payload):
                    for segment in self._maybe_subsplit(para):
                        parent.children.append(
                            _Node(
                                chunk_id=str(uuid.uuid4()),
                                level=child_level,
                                chunk_type=ChunkType.CONTENT,
                                title="",
                                body=segment.strip(),
                                heading_path=list(parent.heading_path),
                                parent=parent,
                            )
                        )
            else:  # image marker
                ref = image_map.get(payload)
                caption = (ref.caption.strip() if ref else "") or "[image]"
                image_meta: dict = {"image_id": payload}
                if ref is not None:
                    image_meta.update(
                        {
                            "mime": ref.mime,
                            "page": ref.page,
                            "image_bytes": len(ref.image_bytes),
                        }
                    )
                    if ref.alt_text:
                        image_meta["alt_text"] = ref.alt_text
                parent.children.append(
                    _Node(
                        chunk_id=str(uuid.uuid4()),
                        level=child_level,
                        chunk_type=ChunkType.IMAGE,
                        title="",
                        body=caption,
                        heading_path=list(parent.heading_path),
                        parent=parent,
                        metadata=image_meta,
                    )
                )

    # ------------------------------------------------------------------
    # Size fallback — only used when a single paragraph is oversized
    # ------------------------------------------------------------------

    def _maybe_subsplit(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if self._max_leaf <= 0 or self._length(text) <= self._max_leaf:
            return [text]

        pieces = self._recursive_split(text, _SUBSPLIT_SEPARATORS)
        return self._merge_with_overlap(pieces)

    def _recursive_split(self, text: str, separators: tuple[str, ...]) -> list[str]:
        if not text:
            return []
        if self._length(text) <= self._max_leaf:
            return [text]

        sep_index = -1
        for i, sep in enumerate(separators):
            if sep and sep in text:
                sep_index = i
                break

        if sep_index == -1:
            char_cap = max(self._max_leaf * 4, 1)
            return [text[i : i + char_cap] for i in range(0, len(text), char_cap)]

        sep = separators[sep_index]
        raw_parts = text.split(sep)
        keep_sep = sep not in ("\n\n", "\n", " ")

        parts: list[str] = []
        for j, part in enumerate(raw_parts):
            if not part:
                continue
            attach = keep_sep and j < len(raw_parts) - 1
            parts.append(part + sep if attach else part)

        out: list[str] = []
        next_seps = separators[sep_index + 1 :]
        for part in parts:
            if self._length(part) <= self._max_leaf:
                out.append(part)
            else:
                out.extend(self._recursive_split(part, next_seps))
        return out

    def _merge_with_overlap(self, pieces: list[str]) -> list[str]:
        if not pieces:
            return []

        chunks: list[str] = []
        current: list[str] = []
        current_size = 0

        for piece in pieces:
            psize = self._length(piece)
            if current and current_size + psize > self._max_leaf:
                chunks.append("".join(current).strip())
                if self._overlap > 0:
                    tail: list[str] = []
                    tail_size = 0
                    for s in reversed(current):
                        s_size = self._length(s)
                        if tail and tail_size + s_size > self._overlap:
                            break
                        tail.insert(0, s)
                        tail_size += s_size
                    current = tail
                    current_size = tail_size
                else:
                    current = []
                    current_size = 0
            current.append(piece)
            current_size += psize

        if current:
            chunks.append("".join(current).strip())
        return [c for c in chunks if c]

    # ------------------------------------------------------------------
    # Flatten tree → DocumentChunk list (parent before children)
    # ------------------------------------------------------------------

    def _flatten(
        self,
        node: _Node,
        parent_id: Optional[str],
        chunks: list[DocumentChunk],
        doc_id: str,
        user_id: str,
        project_id: str,
        base_meta: dict,
    ) -> None:
        if node.chunk_type == ChunkType.DOCUMENT:
            content = node.title
        elif node.chunk_type == ChunkType.HEADING:
            content = node.title
        else:  # CONTENT / IMAGE / TABLE
            content = node.body

        node_meta: dict = {
            **base_meta,
            **node.metadata,
            "document_id": doc_id,
            "chunk_type": node.chunk_type.value,
            "chunk_level": node.level,
            "heading_path": list(node.heading_path),
        }

        chunks.append(
            DocumentChunk(
                chunk_id=node.chunk_id,
                document_id=doc_id,
                user_id=user_id,
                project_id=project_id,
                parent_chunk_id=parent_id,
                chunk_level=node.level,
                chunk_type=node.chunk_type,
                heading_path=list(node.heading_path),
                content=content,
                metadata=node_meta,
            )
        )

        for child in node.children:
            self._flatten(
                node=child,
                parent_id=node.chunk_id,
                chunks=chunks,
                doc_id=doc_id,
                user_id=user_id,
                project_id=project_id,
                base_meta=base_meta,
            )


def _split_paragraphs(text: str) -> Iterable[str]:
    """Yield non-empty paragraphs separated by one-or-more blank lines."""
    for para in re.split(r"\n\s*\n", text):
        stripped = para.strip()
        if stripped:
            yield stripped


# ──────────────────────────────────────────────────────────────────────
# Back-compat: keep the old name so existing imports keep working.
# ``RecursiveTextSplitter`` used to be a flat, size-based splitter. The
# new hierarchical chunker is a strict superset for RAG use-cases.
# ──────────────────────────────────────────────────────────────────────

class RecursiveTextSplitter(HierarchicalChunker):
    """Deprecated alias. Use ``HierarchicalChunker`` directly."""

    def __init__(
        self,
        chunk_size: int = 1024,
        chunk_overlap: int = 64,
        length_function: Optional[Callable[[str], int]] = None,
        separators: Optional[list[str]] = None,  # ignored, kept for signature compat
    ) -> None:
        super().__init__(
            max_leaf_tokens=chunk_size,
            overlap_tokens=chunk_overlap,
            length_function=length_function,
        )
