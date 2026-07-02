"""Backwards-compat shim — canonical location is
``long_term.backends.filesystem.manager``.
"""
from .long_term.backends.filesystem.manager import (  # noqa: F401
    ENTRYPOINT_NAME,
    FRONTMATTER_MAX_LINES,
    MAX_ENTRYPOINT_BYTES,
    MAX_ENTRYPOINT_LINES,
    MAX_MEMORY_FILES,
    MemdirManager,
    format_memory_manifest,
    scan_memory_files,
    truncate_entrypoint_content,
)

__all__ = [
    "ENTRYPOINT_NAME",
    "FRONTMATTER_MAX_LINES",
    "MAX_ENTRYPOINT_BYTES",
    "MAX_ENTRYPOINT_LINES",
    "MAX_MEMORY_FILES",
    "MemdirManager",
    "format_memory_manifest",
    "scan_memory_files",
    "truncate_entrypoint_content",
]
