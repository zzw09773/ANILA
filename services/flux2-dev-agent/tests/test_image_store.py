"""image_store: persist FLUX PNG output and compute public URL."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.image_store import ImageStore


def test_save_writes_png_and_returns_public_url(tmp_path: Path):
    store = ImageStore(
        local_dir=tmp_path,
        public_url_prefix="/uploads/flux",
    )

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # PNG magic + garbage
    url = store.save(png_bytes)

    assert url.startswith("/uploads/flux/")
    assert url.endswith(".png")

    filename = url.rsplit("/", 1)[-1]
    written = tmp_path / filename
    assert written.exists()
    assert written.read_bytes() == png_bytes


def test_save_creates_local_dir_if_missing(tmp_path: Path):
    target = tmp_path / "does" / "not" / "exist"
    store = ImageStore(local_dir=target, public_url_prefix="/uploads/flux")

    store.save(b"\x89PNG\r\n\x1a\n")

    assert target.is_dir()


def test_save_rejects_unknown_format(tmp_path: Path):
    store = ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")

    with pytest.raises(ValueError, match="image"):
        store.save(b"not an image")


def test_filenames_are_unique(tmp_path: Path):
    store = ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")
    png = b"\x89PNG\r\n\x1a\n"

    urls = {store.save(png) for _ in range(10)}

    assert len(urls) == 10


# ── 多格式支援：雲端 SGLang 輸出 JPEG,不是 PNG(2026-07-06 端到端抓到) ──────
def test_save_accepts_jpeg_and_returns_jpg_url(tmp_path: Path):
    store = ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16  # JPEG magic
    url = store.save(jpeg_bytes)
    assert url.endswith(".jpg")
    filename = url.rsplit("/", 1)[-1]
    assert (tmp_path / filename).read_bytes() == jpeg_bytes


def test_save_accepts_webp(tmp_path: Path):
    store = ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")
    webp_bytes = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8
    url = store.save(webp_bytes)
    assert url.endswith(".webp")
