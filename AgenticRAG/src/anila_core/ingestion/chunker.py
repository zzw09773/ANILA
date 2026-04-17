"""Recursive text splitter for the ANILA Core RAG pipeline.

Splits text using a hierarchy of separators, similar to LangChain's
RecursiveCharacterTextSplitter, with Markdown heading awareness.

Splitting priority:
  1. Markdown headings (##, ###, ####)
  2. Double newlines (paragraph boundaries)
  3. Single newlines
  4. Sentence boundaries (.  ?  !)
  5. Character-level fallback
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models.storage import DocumentChunk


# Default separators ordered from coarse to fine
_DEFAULT_SEPARATORS: list[str] = [
    r"\n#{1,6} ",   # Markdown headings
    r"\n\n",        # Paragraph break
    r"\n",          # Line break
    r"(?<=[.?!])\s+",  # Sentence boundary
    r" ",           # Word boundary
    r"",            # Character fallback
]


def _default_length(text: str) -> int:
    """Rough token estimation: characters / 4."""
    return len(text) // 4 or len(text)


@dataclass
class ChunkMeta:
    """Metadata attached to each produced chunk."""

    source_path: str = ""
    document_id: str = ""
    chunk_index: int = 0
    heading: str = ""
    extra: dict = field(default_factory=dict)


class RecursiveTextSplitter:
    """Split text recursively using a hierarchy of separators.

    Args:
        chunk_size:      Target chunk size in tokens (default 512).
        chunk_overlap:   Token overlap between adjacent chunks (default 50).
        length_function: Function to measure text length in tokens.
        separators:      Ordered list of regex separators (coarse → fine).
    """

    def __init__(
        self,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        length_function: Optional[Callable[[str], int]] = None,
        separators: Optional[list[str]] = None,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._length = length_function or _default_length
        self._separators = separators or _DEFAULT_SEPARATORS

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
    ) -> list[DocumentChunk]:
        """Split *text* into overlapping chunks and wrap in DocumentChunk models.

        Args:
            text:        Input text to split.
            metadata:    Base metadata forwarded to every chunk.
            document_id: ID of the parent document.
            user_id:     Owner user ID.
            project_id:  Project scope.

        Returns:
            List of DocumentChunk objects ready for embedding.
        """
        doc_id = document_id or str(uuid.uuid4())
        base_meta = metadata or {}

        raw_chunks = self._split_text(text, self._separators)
        merged = self._merge_splits(raw_chunks)

        result: list[DocumentChunk] = []
        current_heading = ""

        for idx, chunk_text in enumerate(merged):
            # Track the most recent heading seen
            heading_match = re.match(r"^#{1,6} (.+)", chunk_text.lstrip())
            if heading_match:
                current_heading = heading_match.group(1).strip()

            chunk_meta = {
                **base_meta,
                "chunk_index": idx,
                "heading": current_heading,
            }

            result.append(
                DocumentChunk(
                    document_id=doc_id,
                    user_id=user_id,
                    project_id=project_id,
                    content=chunk_text,
                    metadata=chunk_meta,
                )
            )

        return result

    # ------------------------------------------------------------------
    # Internal splitting logic
    # ------------------------------------------------------------------

    def _split_text(self, text: str, separators: list[str]) -> list[str]:
        """Recursively split *text* using *separators* from coarse to fine."""
        if not text.strip():
            return []

        # If already small enough, return as-is
        if self._length(text) <= self._chunk_size:
            return [text]

        # Try separators from coarsest to finest
        for i, sep in enumerate(separators):
            splits = self._split_with_sep(text, sep)
            if len(splits) > 1:
                # Recurse on pieces that are still too large
                result: list[str] = []
                for piece in splits:
                    if self._length(piece) > self._chunk_size:
                        result.extend(self._split_text(piece, separators[i + 1:] or [""]))
                    else:
                        result.append(piece)
                return result

        # Fallback: hard character split
        return self._hard_split(text)

    def _split_with_sep(self, text: str, sep: str) -> list[str]:
        """Split *text* by regex *sep*, keeping separators attached to the
        following piece (so headings stay with their content)."""
        if not sep:
            return [text]

        parts = re.split(f"({sep})", text)
        # Re-join separator with the following piece
        merged: list[str] = []
        i = 0
        while i < len(parts):
            if i + 1 < len(parts) and re.fullmatch(sep, parts[i + 1]):
                merged.append(parts[i] + parts[i + 1])
                i += 2
            else:
                merged.append(parts[i])
                i += 1

        return [p for p in merged if p.strip()]

    def _merge_splits(self, splits: list[str]) -> list[str]:
        """Merge small adjacent splits into chunks, adding overlap."""
        chunks: list[str] = []
        current_pieces: list[str] = []
        current_len = 0

        for split in splits:
            split_len = self._length(split)

            if current_len + split_len > self._chunk_size and current_pieces:
                # Flush current chunk
                chunk_text = "\n".join(current_pieces).strip()
                if chunk_text:
                    chunks.append(chunk_text)

                # Build overlap from end of current buffer
                overlap_pieces: list[str] = []
                overlap_len = 0
                for piece in reversed(current_pieces):
                    piece_len = self._length(piece)
                    if overlap_len + piece_len > self._chunk_overlap:
                        break
                    overlap_pieces.insert(0, piece)
                    overlap_len += piece_len

                current_pieces = overlap_pieces
                current_len = overlap_len

            current_pieces.append(split)
            current_len += split_len

        # Flush remaining
        if current_pieces:
            chunk_text = "\n".join(current_pieces).strip()
            if chunk_text:
                chunks.append(chunk_text)

        return chunks

    def _hard_split(self, text: str) -> list[str]:
        """Last-resort: split by raw character count."""
        step = self._chunk_size * 4  # chars per token ≈ 4
        overlap = self._chunk_overlap * 4
        parts: list[str] = []
        start = 0
        while start < len(text):
            end = start + step
            parts.append(text[start:end])
            start += step - overlap
        return [p for p in parts if p.strip()]
