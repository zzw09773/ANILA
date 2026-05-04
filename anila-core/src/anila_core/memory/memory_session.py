"""Backwards-compat shim — canonical location is ``short_term.in_memory``."""
from .short_term.in_memory import MemorySession  # noqa: F401

__all__ = ["MemorySession"]
