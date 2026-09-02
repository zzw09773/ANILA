"""每頁預覽圖（2026-09-02）。

視覺 QA 本來就截了每一頁的 PNG（渲染器 /screenshots），以前用完就丟。現在存到
``ARTIFACTS_DIR/slides/{job_id}/NN.png``，前端可以在下載前先看、之後可以只重做一張。
沒跑 QA 的 job（安全範本、跳過 QA）第一次被要預覽時再向渲染器要一次。
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from app.config import settings
from app.services.studio_config import RENDERER_BASE_URL

logger = logging.getLogger(__name__)


def preview_dir(job_id: str) -> Path:
    return Path(settings.ARTIFACTS_DIR) / "slides" / job_id


def persist_previews(job_id: str, pngs: list[bytes]) -> int:
    """Best-effort; returns how many were written."""
    d = preview_dir(job_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
        for i, png in enumerate(pngs):
            (d / f"{i:02d}.png").write_bytes(png)
        return len(pngs)
    except OSError as exc:
        logger.warning("could not persist previews for %s: %s", job_id, exc)
        return 0


def count_previews(job_id: str) -> int:
    d = preview_dir(job_id)
    if not d.is_dir():
        return 0
    return len([p for p in d.iterdir() if p.suffix == ".png"])


def load_preview(job_id: str, index: int) -> bytes | None:
    path = preview_dir(job_id) / f"{index:02d}.png"
    try:
        return path.read_bytes()
    except OSError:
        return None


async def screenshots_from_bytes(pptx_bytes: bytes) -> list[bytes]:
    """Ask the renderer for one PNG per slide of an in-memory / on-disk deck."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{RENDERER_BASE_URL}/screenshots",
            json={"pptxBase64": base64.b64encode(pptx_bytes).decode("ascii")},
        )
    r.raise_for_status()
    return [base64.b64decode(img["base64"]) for img in r.json().get("images", [])]


async def ensure_previews(job_id: str, pptx_bytes: bytes | None) -> int:
    """Return the preview count, generating and persisting them if missing."""
    n = count_previews(job_id)
    if n or not pptx_bytes:
        return n
    try:
        pngs = await screenshots_from_bytes(pptx_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("on-demand previews failed for %s: %s", job_id, exc)
        return 0
    return persist_previews(job_id, pngs)
