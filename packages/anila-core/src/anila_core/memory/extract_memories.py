"""Backwards-compat shim — canonical location is
``long_term.backends.filesystem.extractor``.
"""
from .long_term.backends.filesystem.extractor import (  # noqa: F401
    EXTRACT_PROMPT_TEMPLATE,
    EXTRACTION_ALLOWED_TOOLS,
    MemoryExtractor,
)

__all__ = [
    "EXTRACT_PROMPT_TEMPLATE",
    "EXTRACTION_ALLOWED_TOOLS",
    "MemoryExtractor",
]
