"""Persist FLUX PNG output to a shared volume and compute the
public-facing URL.

The local directory is bind-mounted from the host's
``share-dev/uploads/flux/`` into both this container and the
``anila-nginx-dev`` container. Nginx serves it under
``/uploads/flux/`` (sibling of the existing ``/uploads/`` static
route in ``infra/nginx/anila.conf``).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path


def _detect_ext(data: bytes) -> str | None:
    """依 magic bytes 判斷圖片格式的副檔名;非圖片回 None。

    OpenAI 相容 Images API 不保證 PNG —— 雲端 SGLang(FLUX)實測輸出
    JPEG。這裡認幾種常見圖片格式,存對副檔名(nginx 才能回對的
    Content-Type),而不是硬綁 PNG。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return None


@dataclass(frozen=True)
class ImageStore:
    local_dir: Path
    public_url_prefix: str

    def save(self, image_bytes: bytes) -> str:
        ext = _detect_ext(image_bytes)
        if ext is None:
            raise ValueError(
                "payload is not a recognised image (png/jpg/webp/gif magic "
                "bytes missing)"
            )

        Path(self.local_dir).mkdir(parents=True, exist_ok=True)

        filename = f"{uuid.uuid4().hex}.{ext}"
        (Path(self.local_dir) / filename).write_bytes(image_bytes)

        return f"{self.public_url_prefix.rstrip('/')}/{filename}"
