"""Stage 2 quality gate (Layer C) tests.

Covers:
  * ``_has_striping_artifact`` pure-CV detector: a flat/solid image passes,
    a horizontal-stripe (barcode-like) image is flagged.
  * the minimal stdlib PNG decoder used by the striping gate.
  * ``gate_candidates`` orchestration with mock CLIP scorer / VLM:
      - all gates pass -> highest clip_score wins
      - VLM has_text=True -> rejected
      - VLM match=False -> rejected
      - CLIP below threshold -> rejected (using a real low scorer)
      - all candidates fail -> None
"""
from __future__ import annotations

import struct
import zlib

import numpy as np
import pytest

from app.services.flux_image_provider import GeneratedImage
from app.services.flux_quality_gate import (
    CLIP_THRESHOLD,
    _decode_png_to_gray,
    _has_striping_artifact,
    gate_candidates,
    stub_clip_scorer,
)


# ── PNG fixture builder (8-bit RGB, non-interlaced, filter 0) ───────────────
def _make_png(pixels: np.ndarray) -> bytes:
    """Encode an (H, W, 3) uint8 array as a minimal PNG (filter 0)."""
    h, w, _ = pixels.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter: None
        raw.extend(pixels[y].astype(np.uint8).tobytes())
    compressed = zlib.compress(bytes(raw))

    def chunk(ctype: bytes, data: bytes) -> bytes:
        body = ctype + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # color_type 2 = RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def _solid_png(size: int = 64, value: int = 128) -> bytes:
    arr = np.full((size, size, 3), value, dtype=np.uint8)
    return _make_png(arr)


def _striped_png(size: int = 64) -> bytes:
    """Sharp 1-pixel horizontal stripes — a barcode-like high-frequency
    pattern that should trip the striping detector even though the image is
    'flat' in the low-frequency sense."""
    arr = np.zeros((size, size, 3), dtype=np.uint8)
    arr[1::2, :, :] = 255  # every other row white
    return _make_png(arr)


# ── PNG decoder ─────────────────────────────────────────────────────────────
def test_decode_png_roundtrip_solid():
    gray = _decode_png_to_gray(_solid_png(value=200))
    assert gray is not None
    assert gray.shape == (64, 64)
    assert np.allclose(gray, 200.0, atol=1.0)


def test_decode_png_rejects_non_png():
    assert _decode_png_to_gray(b"not a png") is None


# ── striping detector ───────────────────────────────────────────────────────
def test_striping_check_passes_on_solid():
    assert _has_striping_artifact(_solid_png()) is False


def test_striping_check_flags_stripes():
    assert _has_striping_artifact(_striped_png()) is True


def test_striping_check_fails_open_on_bad_bytes():
    # Undecodable -> must NOT flag (fail open; other gates still apply).
    assert _has_striping_artifact(b"\x89PNG\r\n\x1a\ngarbage") is False


# ── mock VLM ────────────────────────────────────────────────────────────────
class _MockVlm:
    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict
        self.calls = 0

    async def check(self, png_bytes: bytes, *, concept: str) -> dict:  # noqa: ARG002
        self.calls += 1
        return dict(self._verdict)


def _candidate() -> GeneratedImage:
    return GeneratedImage(png_bytes=_solid_png(), seed=1, accepted=False)


# ── gate_candidates ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gate_all_pass_picks_highest_clip():
    c_low = _candidate()
    c_high = _candidate()

    def scorer(png, prompt):  # noqa: ANN001, ARG001
        # First call -> low, second -> high (map by call order).
        return scorer._queue.pop(0)

    scorer._queue = [30.0, 90.0]
    vlm = _MockVlm({"match": True, "has_text": False, "reason": "ok"})

    best = await gate_candidates(
        [c_low, c_high],
        flux_prompt="abstract teal swirl",
        concept_en="abstract teal swirl",
        clip_scorer=scorer,
        vlm=vlm,
    )
    assert best is not None
    assert best.clip_score == 90.0
    assert best.accepted is True


@pytest.mark.asyncio
async def test_gate_rejects_has_text():
    vlm = _MockVlm({"match": True, "has_text": True, "reason": "logo"})
    best = await gate_candidates(
        [_candidate()],
        flux_prompt="p",
        concept_en="c",
        clip_scorer=stub_clip_scorer,
        vlm=vlm,
    )
    assert best is None


@pytest.mark.asyncio
async def test_gate_rejects_no_match():
    vlm = _MockVlm({"match": False, "has_text": False, "reason": "off-concept"})
    best = await gate_candidates(
        [_candidate()],
        flux_prompt="p",
        concept_en="c",
        clip_scorer=stub_clip_scorer,
        vlm=vlm,
    )
    assert best is None


@pytest.mark.asyncio
async def test_gate_rejects_low_clip_before_vlm():
    vlm = _MockVlm({"match": True, "has_text": False})

    def low_scorer(png, prompt):  # noqa: ANN001, ARG001
        return CLIP_THRESHOLD - 1.0

    best = await gate_candidates(
        [_candidate()],
        flux_prompt="p",
        concept_en="c",
        clip_scorer=low_scorer,
        vlm=vlm,
    )
    assert best is None
    # CLIP gate short-circuits before the VLM round-trip.
    assert vlm.calls == 0


@pytest.mark.asyncio
async def test_gate_rejects_striping_before_vlm():
    striped = GeneratedImage(png_bytes=_striped_png(), seed=1, accepted=False)
    vlm = _MockVlm({"match": True, "has_text": False})
    best = await gate_candidates(
        [striped],
        flux_prompt="p",
        concept_en="c",
        clip_scorer=stub_clip_scorer,  # passes CLIP
        vlm=vlm,
    )
    assert best is None
    assert vlm.calls == 0  # striping gate is before VLM


@pytest.mark.asyncio
async def test_gate_empty_candidates_returns_none():
    best = await gate_candidates(
        [],
        flux_prompt="p",
        concept_en="c",
        clip_scorer=stub_clip_scorer,
        vlm=_MockVlm({"match": True, "has_text": False}),
    )
    assert best is None


@pytest.mark.asyncio
async def test_stub_clip_scorer_passes_threshold():
    assert stub_clip_scorer(_solid_png(), "anything") >= CLIP_THRESHOLD
