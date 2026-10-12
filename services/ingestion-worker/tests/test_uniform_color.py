"""Uniform-color image filter — skip PDF background rectangles that
have no informational content (avoid wasting VLM calls + polluting RAG)."""
from __future__ import annotations

import pytest
import io
import random

from PIL import Image

from ingestion_worker.handlers import _is_uniform_color


def _png_bytes(color: tuple, size: tuple = (256, 256)) -> bytes:
    """Build a solid-color PNG."""
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _gradient_png(size: tuple = (256, 256)) -> bytes:
    """A gradient PNG — has real variance."""
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    "item, item2, item3",
    [
        pytest.param(26, 54, 93, id='solid_blue_is_uniform'),
        pytest.param(255, 255, 255, id='solid_white_is_uniform'),
    ],
)
def test_solid_blue_is_uniform_cases(item, item2, item3):
    assert _is_uniform_color(_png_bytes((item, item2, item3))) is True


def test_gradient_is_not_uniform():
    assert _is_uniform_color(_gradient_png()) is False


def test_near_uniform_within_tolerance_is_uniform():
    """A slightly noisy solid color (jpg compression artifacts) — still uniform."""
    img = Image.new("RGB", (256, 256))
    rng = random.Random(0)
    for x in range(256):
        for y in range(256):
            img.putpixel((x, y), (
                26 + rng.randint(-3, 3),
                54 + rng.randint(-3, 3),
                93 + rng.randint(-3, 3),
            ))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert _is_uniform_color(buf.getvalue(), tolerance=10) is True


def test_tiny_icon_skipped_from_filter():
    """Tiny images (< 100 pixels total) bypass the uniform check —
    icons are intentionally small + low-variance."""
    icon = _png_bytes((100, 100, 100), size=(8, 8))
    # Even though uniform, returns False so it gets through.
    assert _is_uniform_color(icon) is False


@pytest.mark.parametrize(
    "is_uniform_color_arg",
    [
        # Garbage bytes — let downstream handle. Don't claim uniform.
        pytest.param(b"not an image", id='decode_failure_returns_false'),
        pytest.param(b"", id='empty_bytes_returns_false'),
    ],
)
def test_decode_failure_returns_false_cases(is_uniform_color_arg):
    assert _is_uniform_color(is_uniform_color_arg) is False


