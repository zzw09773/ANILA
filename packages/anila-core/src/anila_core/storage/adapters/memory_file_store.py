"""File-system backed MemoryStore adapter.

Integrates with the existing memdir.py frontmatter format.
Memory files live on disk as Markdown with YAML frontmatter,
consistent with Claude Code's memory conventions.

Implements the MemoryStore Protocol defined in storage/ports.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import frontmatter  # type: ignore[import]

from ...models.memory import MemoryFile, MemoryHeader

logger = logging.getLogger(__name__)


class MemoryFileStore:
    """MemoryStore that reads/writes Markdown+frontmatter files on disk.

    Args:
        base_dir: Root directory for memory files.
                  Files are organised as {base_dir}/{user_id}/{project_id}/*.md
    """

    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir)

    # ------------------------------------------------------------------
    # MemoryStore Protocol
    # ------------------------------------------------------------------

    async def read(self, file_path: str) -> Optional[MemoryFile]:
        """Read a memory file by absolute path."""
        path = Path(file_path)
        if not path.exists():
            return None
        try:
            post = frontmatter.load(str(path))
            raw = frontmatter.dumps(post)
            header = MemoryHeader.from_dict(
                data=dict(post.metadata),
                filename=path.name,
                file_path=file_path,
                mtime_ms=path.stat().st_mtime * 1000,
            )
            return MemoryFile(
                header=header,
                body=post.content,
                frontmatter_raw=raw,
            )
        except Exception as exc:
            logger.warning("Failed to read memory file %s: %s", file_path, exc)
            return None

    async def write(self, file_path: str, memory: MemoryFile) -> None:
        """Write a memory file, creating parent directories as needed."""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(memory.to_markdown(), encoding="utf-8")

    async def list_headers(
        self,
        user_id: str,
        project_id: str,
        scope: str = "project",
    ) -> list[MemoryHeader]:
        """List memory headers for a given scope."""
        scope_dir = self._base / user_id / project_id / scope
        if not scope_dir.exists():
            return []

        headers: list[MemoryHeader] = []
        for md_file in sorted(scope_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(md_file))
                headers.append(
                    MemoryHeader.from_dict(
                        data=dict(post.metadata),
                        filename=md_file.name,
                        file_path=str(md_file),
                        mtime_ms=md_file.stat().st_mtime * 1000,
                    )
                )
            except Exception as exc:
                logger.warning("Skipping malformed memory file %s: %s", md_file, exc)
        return headers

    async def delete(self, file_path: str) -> None:
        """Delete a memory file if it exists."""
        path = Path(file_path)
        if path.exists():
            path.unlink()
