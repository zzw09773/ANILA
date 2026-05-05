"""Lightweight logging setup. Honours ANILA_LOG_LEVEL."""

from __future__ import annotations

import logging
import os

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure(level: str | None = None) -> None:
    """Configure root logger once. Idempotent."""
    resolved = (level or os.environ.get("ANILA_LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(resolved)
        return
    logging.basicConfig(level=resolved, format=_DEFAULT_FORMAT)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
