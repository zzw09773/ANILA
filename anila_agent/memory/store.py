"""File-backed memory directory. Faithful Python port of claude-code-src `memdir/`.

Layout:

    <memory_dir>/
        MEMORY.md              # one-line index, capped at 200 lines / 25KB
        feedback_testing.md    # topic file with YAML frontmatter
        project_release.md
        ...

Each topic file has frontmatter:

    ---
    name: short title
    description: one-line description used by the recall selector
    type: user|feedback|project|reference
    ---

    free-form markdown content
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from anila_agent.models.schemas import MemoryFrontmatter

ENTRYPOINT_NAME = "MEMORY.md"
DEFAULT_MAX_LINES = 200
DEFAULT_MAX_BYTES = 25_000
DEFAULT_MAX_FILES = 200
FRONTMATTER_MAX_LINES = 30
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", flags=re.DOTALL)


@dataclass(frozen=True)
class MemoryHeader:
    """Metadata for a single memory file. Returned from `scan()`."""

    filename: str
    path: Path
    mtime: float
    description: str | None
    type: str | None


@dataclass(frozen=True)
class TruncatedIndex:
    content: str
    line_count: int
    byte_count: int
    line_truncated: bool
    byte_truncated: bool


class MemdirStore:
    """Plain CRUD on the memory directory. No LLM calls — that lives in `LongTermMemory`."""

    def __init__(
        self,
        memory_dir: str | Path,
        *,
        max_index_lines: int = DEFAULT_MAX_LINES,
        max_index_bytes: int = DEFAULT_MAX_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.dir = Path(memory_dir).expanduser().resolve()
        self.max_index_lines = max_index_lines
        self.max_index_bytes = max_index_bytes
        self.max_files = max_files
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def index_path(self) -> Path:
        return self.dir / ENTRYPOINT_NAME

    def read_index(self) -> str:
        try:
            return self.index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def write_index(self, content: str) -> None:
        self.index_path.write_text(content.rstrip() + "\n", encoding="utf-8")

    def append_index_entry(self, line: str) -> None:
        """Add `line` to MEMORY.md if not already present."""
        line = line.strip()
        if not line:
            return
        existing = self.read_index().rstrip()
        if line in existing.splitlines():
            return
        new = (existing + "\n" + line).strip()
        self.write_index(new)

    def truncate_index(self, raw: str | None = None) -> TruncatedIndex:
        """Apply line + byte caps. Faithful to claude-code-src truncateEntrypointContent."""
        text = (raw if raw is not None else self.read_index()).strip()
        lines = text.split("\n")
        line_count = len(lines)
        byte_count = len(text.encode("utf-8"))
        line_truncated = line_count > self.max_index_lines
        byte_truncated = byte_count > self.max_index_bytes
        if not (line_truncated or byte_truncated):
            return TruncatedIndex(text, line_count, byte_count, False, False)
        truncated = "\n".join(lines[: self.max_index_lines]) if line_truncated else text
        if len(truncated.encode("utf-8")) > self.max_index_bytes:
            cut_at = truncated.rfind("\n", 0, self.max_index_bytes)
            truncated = truncated[: cut_at if cut_at > 0 else self.max_index_bytes]
        return TruncatedIndex(
            content=truncated, line_count=line_count, byte_count=byte_count,
            line_truncated=line_truncated, byte_truncated=byte_truncated,
        )

    def scan(self) -> list[MemoryHeader]:
        """Walk the memory dir, parse frontmatter, sort newest-first, cap at max_files."""
        if not self.dir.exists():
            return []
        out: list[MemoryHeader] = []
        for path in self.dir.rglob("*.md"):
            if path.name == ENTRYPOINT_NAME:
                continue
            try:
                rel = path.relative_to(self.dir).as_posix()
                stat = path.stat()
                content = self._read_head(path, FRONTMATTER_MAX_LINES)
                fm = self.parse_frontmatter(content)
                desc = fm.get("description") if fm else None
                ftype = fm.get("type") if fm else None
                out.append(
                    MemoryHeader(
                        filename=rel,
                        path=path,
                        mtime=stat.st_mtime,
                        description=desc if isinstance(desc, str) else None,
                        type=ftype if isinstance(ftype, str) else None,
                    )
                )
            except OSError:
                continue
        out.sort(key=lambda h: h.mtime, reverse=True)
        return out[: self.max_files]

    @staticmethod
    def _read_head(path: Path, max_lines: int) -> str:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head: list[str] = []
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                head.append(line)
        return "".join(head)

    @staticmethod
    def parse_frontmatter(text: str) -> dict[str, object]:
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return {}
        try:
            data = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def format_manifest(headers: Iterable[MemoryHeader]) -> str:
        from datetime import datetime, timezone

        out: list[str] = []
        for h in headers:
            tag = f"[{h.type}] " if h.type else ""
            ts = datetime.fromtimestamp(h.mtime, tz=timezone.utc).isoformat(timespec="seconds")
            base = f"- {tag}{h.filename} ({ts})"
            out.append(f"{base}: {h.description}" if h.description else base)
        return "\n".join(out)

    def read(self, filename: str) -> tuple[MemoryFrontmatter | None, str]:
        """Return (frontmatter, body) for a file relative to the memory dir."""
        path = self.dir / filename
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return None, text
        try:
            data = yaml.safe_load(m.group(1)) or {}
            fm = MemoryFrontmatter.model_validate(data)
        except (yaml.YAMLError, ValueError):
            return None, text
        body = text[m.end() :]
        return fm, body

    def write(
        self,
        filename: str,
        frontmatter: MemoryFrontmatter,
        body: str,
    ) -> Path:
        """Write a memory file with frontmatter. Filename is relative to memory dir."""
        if os.path.isabs(filename) or ".." in Path(filename).parts:
            raise ValueError(f"filename must be relative and inside the memory dir: {filename!r}")
        path = self.dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_yaml = yaml.safe_dump(
            frontmatter.model_dump(), sort_keys=False, allow_unicode=True
        ).strip()
        rendered = f"---\n{fm_yaml}\n---\n\n{body.strip()}\n"
        path.write_text(rendered, encoding="utf-8")
        return path

    def delete(self, filename: str) -> bool:
        path = self.dir / filename
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
