"""Deterministic geometric QA — call renderer's /qa-geometric and
return defects compatible with the existing VisualDefect shape so
the rest of the pipeline doesn't care that the source isn't vision-LLM.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class GeometricDefect:
    slide_index: int
    severity: str  # "warning" | "critical"
    kind: str      # "whitespace" | "overflow" | "overlap" | "text_density"
    detail: str


async def run_geometric_qa(
    pptx_bytes: bytes,
    *,
    renderer_url: str | None = None,
    timeout: float = 30.0,
) -> list[GeometricDefect]:
    """POST pptx to renderer /qa-geometric, return parsed defect list.

    On any error (renderer down, timeout, malformed response), return
    empty list — geometric QA is best-effort; the vision QA layer
    will still cover. Logs the failure.
    """
    url = renderer_url or os.environ.get("RENDERER_BASE_URL", "http://pptx-renderer:7100")
    payload = {"pptxBase64": base64.b64encode(pptx_bytes).decode("ascii")}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{url.rstrip('/')}/qa-geometric", json=payload)
        if resp.status_code != 200:
            logger.warning("geometric QA returned %s: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("geometric QA call failed: %s", exc)
        return []
    out: list[GeometricDefect] = []
    for d in data.get("defects", []):
        try:
            out.append(
                GeometricDefect(
                    slide_index=int(d["slide_index"]),
                    severity=str(d["severity"]),
                    kind=str(d["kind"]),
                    detail=str(d.get("detail", "")),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out
