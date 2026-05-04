"""Memdir — MEMORY.md index management and memory prompt injection.

Ported from Claude Code memdir.ts and memoryScan.ts.

MEMORY.md is the entrypoint index. Each line is:
  - [Title](file.md) - one-line hook

Caps:
  - MAX_ENTRYPOINT_LINES = 200
  - MAX_ENTRYPOINT_BYTES = 25_000

Memory files are .md files with YAML frontmatter containing
title, description, type, tags, created, updated, scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from anila_core.models.memory import MemoryHeader

logger = logging.getLogger(__name__)

ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000
MAX_MEMORY_FILES = 200
FRONTMATTER_MAX_LINES = 30


def _parse_frontmatter_from_text(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from the beginning of a markdown file."""
    stripped = text.strip()
    if not stripped.startswith("---"):
        return {}, stripped

    rest = stripped[3:]
    end = rest.find("\n---")
    if end == -1:
        return {}, stripped

    yaml_block = rest[:end].strip()
    body = rest[end + 4:].strip()

    try:
        data = yaml.safe_load(yaml_block) or {}
        return data, body
    except yaml.YAMLError:
        return {}, stripped


async def scan_memory_files(
    memory_dir: str,
    signal: Optional[asyncio.Event] = None,
) -> list[MemoryHeader]:
    """Scan a memory directory for .md files and read their frontmatter.

    Returns headers sorted by mtime descending (newest first),
    capped at MAX_MEMORY_FILES.

    Args:
        memory_dir: Directory to scan.
        signal: Optional abort event.
    """
    mem_path = Path(memory_dir)
    if not mem_path.is_dir():
        return []

    md_files: list[Path] = []
    try:
        for entry in mem_path.rglob("*.md"):
            if entry.name == ENTRYPOINT_NAME:
                continue
            if signal and signal.is_set():
                return []
            md_files.append(entry)
    except OSError:
        return []

    headers: list[MemoryHeader] = []
    for file_path in md_files:
        if signal and signal.is_set():
            return []
        try:
            stat = file_path.stat()
            mtime_ms = stat.st_mtime_ns / 1_000_000
            # Read just the first N lines for frontmatter
            lines: list[str] = []
            with open(file_path, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i >= FRONTMATTER_MAX_LINES:
                        break
                    lines.append(line)
            partial_text = "".join(lines)
            fm_data, _ = _parse_frontmatter_from_text(partial_text)
            relative_name = str(file_path.relative_to(mem_path))
            headers.append(
                MemoryHeader.from_dict(
                    fm_data,
                    filename=relative_name,
                    file_path=str(file_path),
                    mtime_ms=mtime_ms,
                )
            )
        except OSError:
            continue

    # Sort newest first, cap at MAX_MEMORY_FILES
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_MEMORY_FILES]


def format_memory_manifest(headers: list[MemoryHeader]) -> str:
    """Format memory headers as a manifest string for prompts."""
    lines = [h.format_manifest_line() for h in headers]
    return "\n".join(lines)


def truncate_entrypoint_content(raw: str) -> tuple[str, bool, bool]:
    """Truncate MEMORY.md content to line and byte caps.

    Returns (truncated_content, was_line_truncated, was_byte_truncated).
    """
    trimmed = raw.strip()
    lines = trimmed.split("\n")
    line_count = len(lines)
    byte_count = len(trimmed.encode("utf-8"))

    was_line_truncated = line_count > MAX_ENTRYPOINT_LINES
    was_byte_truncated = byte_count > MAX_ENTRYPOINT_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return trimmed, False, False

    truncated = "\n".join(lines[:MAX_ENTRYPOINT_LINES]) if was_line_truncated else trimmed

    encoded = truncated.encode("utf-8")
    if len(encoded) > MAX_ENTRYPOINT_BYTES:
        cut = truncated[:MAX_ENTRYPOINT_BYTES].rfind("\n")
        truncated = truncated[:cut] if cut > 0 else truncated[:MAX_ENTRYPOINT_BYTES]

    reason_parts = []
    if was_line_truncated:
        reason_parts.append(f"{line_count} lines (limit: {MAX_ENTRYPOINT_LINES})")
    if was_byte_truncated:
        reason_parts.append(f"{byte_count} bytes (limit: {MAX_ENTRYPOINT_BYTES})")
    reason = " and ".join(reason_parts)

    warning = (
        f"\n\n> WARNING: {ENTRYPOINT_NAME} is {reason}. Only part of it was loaded. "
        "Keep index entries to one line under ~150 chars; move detail into topic files."
    )
    return truncated + warning, was_line_truncated, was_byte_truncated


class MemdirManager:
    """Manages a memory directory and its MEMORY.md index."""

    def __init__(self, memory_dir: str) -> None:
        self._dir = memory_dir
        self._entrypoint = os.path.join(memory_dir, ENTRYPOINT_NAME)

    @property
    def memory_dir(self) -> str:
        return self._dir

    @property
    def entrypoint_path(self) -> str:
        return self._entrypoint

    def ensure_dir(self) -> None:
        """Create the memory directory if it does not exist."""
        os.makedirs(self._dir, exist_ok=True)

    def read_entrypoint(self) -> str:
        """Read MEMORY.md content, returning empty string if absent."""
        try:
            with open(self._entrypoint, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def write_entrypoint(self, content: str) -> None:
        """Write MEMORY.md content."""
        self.ensure_dir()
        with open(self._entrypoint, "w", encoding="utf-8") as fh:
            fh.write(content)

    def build_memory_prompt(self) -> str:
        """Return the memory prompt text to inject into the system prompt."""
        raw = self.read_entrypoint()
        lines = [
            "# auto memory",
            "",
            f"You have a persistent, file-based memory system at `{self._dir}`. "
            "This directory already exists — write to it directly with the Write tool.",
            "",
        ]

        if raw.strip():
            truncated, _, _ = truncate_entrypoint_content(raw)
            lines.extend([f"## {ENTRYPOINT_NAME}", "", truncated])
        else:
            lines.extend([
                f"## {ENTRYPOINT_NAME}",
                "",
                f"Your {ENTRYPOINT_NAME} is currently empty. "
                "When you save new memories, they will appear here.",
            ])

        return "\n".join(lines)

    async def scan(self, signal: Optional[asyncio.Event] = None) -> list[MemoryHeader]:
        """Scan the memory directory and return all MemoryHeader records."""
        return await scan_memory_files(self._dir, signal)

    def add_index_entry(self, title: str, filename: str, hook: str) -> None:
        """Append a new entry to MEMORY.md, respecting the line cap."""
        raw = self.read_entrypoint()
        lines = raw.strip().split("\n") if raw.strip() else []
        new_entry = f"- [{title}]({filename}) - {hook}"

        # Check for duplicate
        if any(filename in line for line in lines):
            return

        if len(lines) >= MAX_ENTRYPOINT_LINES:
            # Remove oldest entry to make room
            lines = lines[1:]

        lines.append(new_entry)
        self.write_entrypoint("\n".join(lines) + "\n")
