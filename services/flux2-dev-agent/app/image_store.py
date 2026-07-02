"""Persist FLUX PNG output to a shared volume and compute the
public-facing URL.

The local directory is bind-mounted from the host's
``share-dev/uploads/flux/`` into both this container and the
``anila-nginx-dev`` container. Nginx serves it under
``/uploads/flux/`` (sibling of the existing ``/uploads/`` static
route in ``myCSPPlatform/docker/nginx.conf``).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ImageStore:
    local_dir: Path
    public_url_prefix: str

    def save(self, png_bytes: bytes) -> str:
        if not png_bytes.startswith(_PNG_MAGIC):
            raise ValueError("payload is not a PNG (magic bytes missing)")

        Path(self.local_dir).mkdir(parents=True, exist_ok=True)

        filename = f"{uuid.uuid4().hex}.png"
        (Path(self.local_dir) / filename).write_bytes(png_bytes)

        return f"{self.public_url_prefix.rstrip('/')}/{filename}"
