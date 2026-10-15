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
                "unsupported_objects",
                "presentation_recovery",
                "page_location_quality",
            )
            if key in metadata
        }

        source_offset = metadata.get("_source_offset_chars", 0)
        declared_pages = metadata.get("page_count") or metadata.get("pages")
        if (
            metadata.get("format") == "pdf"
            and declared_pages
            and len(text.split("\f")) != declared_pages
        ):
            source_meta.pop("page", None)
            source_meta.pop("total_pages", None)
            source_meta.update(
                {
                    "page_location_quality": "unknown",
                    "declared_total_pages": declared_pages,
                    "quality_notice": "解析文字沒有可靠的逐頁對應，頁碼未知；請核對原檔。",
                }
            )

        def run(content: str, offset: int, context: list[tuple[int, str]]) -> list[ChunkResult]:
            if self.name == "semantic":
                return (
                    [
                        atomic(
                            content,
                            "structural",
                            {
                                "structure": "block",
                                "heading_path": [title for _, title in context],
                                "offset_chars": source_offset + offset,
                            },
                        )
                    ]
                    if content.strip()
                    else []
                )
            chunks = method(self, content, {**metadata, "_heading_context": context}, params)
            return [
                replace(
                    c,
                    metadata={
                        **({"heading_path": [title for _, title in context]} if context else {}),
                        **c.metadata,
                        **(
                            {"offset_chars": source_offset + offset + c.metadata["offset_chars"]}
                            if "offset_chars" in c.metadata
                            else {}
                        ),
                    },
                )
                for c in chunks
            ]

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
            offset = 0
            for page, content in enumerate(pages, 1):
                page_offset = offset
                offset += len(content) + 1
                if not content.strip():
                    continue
                extra = {
                    "page": page,
                    "total_pages": count,
                    "offset_chars": source_offset + page_offset,
                }
                if metadata.get("slide_count") or self.name == "semantic":
                    if metadata.get("slide_count"):
                        extra["slide"] = page
                    out.append(atomic(content, f"slide-{page:04d}", extra))
                else:
                    inner = {
                        **metadata,
                        **extra,
                        "_source_offset_chars": source_offset + page_offset,
                    }
                    for key in ("page_count", "pages"):
                        inner.pop(key, None)
                    out.extend(_prefix(chunk(self, content, inner, params), f"page-{page:04d}-"))
            return out

        if self.name == "markdown-aware" and _tokens(text) <= int(
            params.get("max_leaf_tokens", 1024)
        ):
            return [
                replace(c, metadata={**c.metadata, **source_meta})
                for c in run(text, 0, list(metadata.get("_heading_context", [])))
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
            heading: list[tuple[int, str]] = list(metadata.get("_heading_context", []))
            for i, block in enumerate(blocks):
                before = text[cursor : block.start()]
                if before.strip():
                    out.extend(_prefix(run(before, cursor, heading), f"part-{i:04d}-"))
                for match in re.finditer(r"^(#{1,6})\s+(.+)$", before, re.MULTILINE):
                    level = len(match.group(1))
                    heading = [(depth, title) for depth, title in heading if depth < level] + [
                        (level, match.group(2).strip())
                    ]
                out.append(
                    atomic(
                        block.group(),
                        f"block-{i:04d}",
                        {
                            "structure": "block",
                            "heading_path": [title for _, title in heading],
                            "offset_chars": source_offset + block.start(),
                        },
                    )
                )
                cursor = block.end()
            if text[cursor:].strip():
                out.extend(_prefix(run(text[cursor:], cursor, heading), "tail-"))
            return [replace(c, metadata={**c.metadata, **source_meta}) for c in out]

        return [
            replace(
                c,
                metadata={
                    **c.metadata,
                    **source_meta,
                    **(
                        {"offset_chars": source_offset + c.metadata["offset_chars"]}
                        if "offset_chars" in c.metadata
                        else {}
                    ),
                },
            )
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
