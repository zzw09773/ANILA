"""Filesystem roots shared by CSP's ingestion and attachment consumers.

The ingestion worker receives the same ``UPLOAD_DIR`` value from compose.
Keeping the CSP-side declaration here prevents upload and disk-alert paths
from drifting apart while still allowing deployment-specific mounts.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_INGESTION_UPLOAD_DIR = "/var/anila/ingestion-uploads"
INGESTION_UPLOAD_ROOT = Path(
    os.getenv("UPLOAD_DIR") or DEFAULT_INGESTION_UPLOAD_DIR
)
ATTACHMENT_STORAGE_ROOT = Path("data/attachments")
