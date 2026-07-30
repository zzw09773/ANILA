"""Re-export the shared upstream URL joiner.

Implementation lives in ``anila_core.security.upstream_urls`` so CSP and
ingestion-worker (and any other outbound caller) share one normalisation
rule. Keep this module as the CSP-local import path used by existing call
sites.
"""
from __future__ import annotations

from anila_core.security.upstream_urls import (
    join_upstream_path,
    strip_trailing_api_version,
)

__all__ = ["join_upstream_path", "strip_trailing_api_version"]
