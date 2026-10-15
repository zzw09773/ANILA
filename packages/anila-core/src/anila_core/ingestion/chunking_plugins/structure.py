"""Common structure and citation protection for built-in chunk strategies."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any, Callable

from .base import ChunkResult


@dataclass(frozen=True)
class _BlockSpan:
    first: int
    last: int
    content: str
    kind: str = "table"

    def start(self) -> int:
        return self.first

    def end(self) -> int:
        return self.last

    def group(self) -> str:
        return self.content


def _fence(line: str) -> tuple[str, int, str] | None:
    body = line.rstrip("\r\n")
    stripped = body.lstrip(" ")
    if len(body) - len(stripped) > 3 or not stripped or stripped[0] not in "`~":
        return None
    char = stripped[0]
    length = len(stripped) - len(stripped.lstrip(char))
    return (char, length, stripped[length:]) if length >= 3 else None


def _structural_blocks(text: str) -> list[_BlockSpan]:
    """Scan each line once, retaining an unclosed fence through end of input."""
    lines = text.splitlines(keepends=True)
    blocks: list[_BlockSpan] = []
    index = offset = 0
    while index < len(lines):
        opened = _fence(lines[index])
        first = offset
        if opened is not None:
            index += 1
            offset += len(lines[index - 1])
            while index < len(lines):
                closed = _fence(lines[index])
                offset += len(lines[index])
                index += 1
                if (
                    closed is not None
                    and closed[0] == opened[0]
                    and closed[1] >= opened[1]
                    and not closed[2].strip()
                ):
                    break
            blocks.append(_BlockSpan(first, offset, text[first:offset], "fence"))
            continue
        row = lines[index].strip()
        if row.startswith("|") and row.endswith("|") and index + 1 < len(lines):
            separator = lines[index + 1].strip()
            if (
                separator.startswith("|")
                and separator.endswith("|")
                and "-" in separator
                and not separator.strip("|:- \t")
            ):
                while index < len(lines):
                    row = lines[index].strip()
                    if not (row.startswith("|") and row.endswith("|")):
                        break
                    offset += len(lines[index])
                    index += 1
                blocks.append(_BlockSpan(first, offset, text[first:offset]))
                continue
        offset += len(lines[index])
        index += 1
    return blocks


def mask_fenced_code(text: str) -> str:
    """Replace fenced source with equal-length spaces without regex backtracking."""
    pieces: list[str] = []
    cursor = 0
    for block in _structural_blocks(text):
        if block.kind != "fence":
            continue
        pieces.extend([text[cursor : block.first], " " * (block.last - block.first)])
        cursor = block.last
    pieces.append(text[cursor:])
    return "".join(pieces)


def protect_structure(method: Callable[..., list[ChunkResult]]) -> Callable[..., list[ChunkResult]]:
    """Keep atomic blocks intact and carry source locations into every chunk."""

    @wraps(method)
    def chunk(
        self: Any, text: str, metadata: dict[str, Any], params: dict[str, Any]
    ) -> list[ChunkResult]:
        from .builtins import _tokens

        source_meta = {
            key: metadata[key]
            for key in (
                "source_filename",
                "format",
                "page",
                "slide",
                "total_pages",
                "actual_parser",
                "quality",
                "quality_notice",
                "parser_fallback",
                "fallback_reason",
                "ocr_used",
                "ocr_notice",
                "image_ocr_error",
                "conversion",
                "converted_from",
            )
            if key in metadata
        }

        def run(content: str) -> list[ChunkResult]:
            if self.name == "semantic":
                return (
                    [atomic(content, "structural", {"structure": "block"})]
                    if content.strip()
                    else []
                )
            return method(self, content, metadata, params)

        def atomic(content: str, key: str, extra: dict[str, Any]) -> ChunkResult:
            return ChunkResult(
                content,
                key,
                _tokens(content),
                {**source_meta, "chunk_type": "leaf", "strategy": self.name, **extra},
            )

        units = metadata.get("source_units")
        if isinstance(units, list):
            lines = text.splitlines(keepends=True)
            return [
                atomic(
                    "".join(lines[u["line_start"] - 1 : u["line_end"]]),
                    f"source-{i:05d}",
                    {
                        **u,
                        "source_language": metadata.get("source_language"),
                        "structure": "source",
                    },
                )
                for i, u in enumerate(units)
                if "".join(lines[u["line_start"] - 1 : u["line_end"]]).strip()
            ]

        pages = text.split("\f")
        count = metadata.get("page_count") or metadata.get("pages")
        if count and len(pages) == count and metadata.get("format") in {"pdf", "ppt", "pptx"}:
            out: list[ChunkResult] = []
            for page, content in enumerate(pages, 1):
                if not content.strip():
                    continue
                extra = {"page": page, "total_pages": count}
                if metadata.get("slide_count") or self.name == "semantic":
                    if metadata.get("slide_count"):
                        extra["slide"] = page
                    out.append(atomic(content, f"slide-{page:04d}", extra))
                else:
                    inner = {**metadata, **extra}
                    for key in ("page_count", "pages"):
                        inner.pop(key, None)
                    out.extend(_prefix(chunk(self, content, inner, params), f"page-{page:04d}-"))
            return out

        if self.name == "markdown-aware" and _tokens(text) <= int(
            params.get("max_leaf_tokens", 1024)
        ):
            return [
                replace(c, metadata={**c.metadata, **source_meta})
                for c in method(self, text, metadata, params)
            ]
        blocks = _structural_blocks(text)
        for table in metadata.get("table_blocks", []):
            if table.strip():
                blocks.extend(
                    _BlockSpan(match.start(), match.end(), match.group())
                    for match in re.finditer(re.escape(table), text)
                )
        blocks.sort(key=lambda match: match.start())
        deduplicated: list[_BlockSpan] = []
        for block in blocks:
            if not deduplicated or block.start() >= deduplicated[-1].end():
                deduplicated.append(block)
        blocks = deduplicated
        if blocks:
            out = []
            cursor = 0
            heading: list[str] = []
            for i, block in enumerate(blocks):
                before = text[cursor : block.start()]
                if before.strip():
                    out.extend(_prefix(run(before), f"part-{i:04d}-"))
                for match in re.finditer(r"^(#{1,6})\s+(.+)$", before, re.MULTILINE):
                    level = len(match.group(1))
                    heading = heading[: level - 1] + [match.group(2)]
                out.append(
                    atomic(
                        block.group(),
                        f"block-{i:04d}",
                        {"structure": "block", "heading_path": heading.copy()},
                    )
                )
                cursor = block.end()
            if text[cursor:].strip():
                out.extend(_prefix(run(text[cursor:]), "tail-"))
            return [replace(c, metadata={**c.metadata, **source_meta}) for c in out]

        return [
            replace(c, metadata={**c.metadata, **source_meta})
            for c in method(self, text, metadata, params)
        ]

    return chunk


def _prefix(chunks: list[ChunkResult], prefix: str) -> list[ChunkResult]:
    out = []
    for chunk in chunks:
        metadata = dict(chunk.metadata)
        if metadata.get("parent_chunk_key"):
            metadata["parent_chunk_key"] = prefix + metadata["parent_chunk_key"]
        out.append(replace(chunk, chunk_key=prefix + chunk.chunk_key, metadata=metadata))
    return out
