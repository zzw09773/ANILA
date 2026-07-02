"""FLUX quality gate — Layer C (Stage 2).

The "把關 (gatekeeper)" that Stage 1 deliberately left as pass-through.
Given N candidate images from the FLUX service, run two independent
gates and return the single best accepted candidate (or ``None`` when all
candidates fail, so the caller can retry or fall back).

Two gates (spec 5.5), cheapest-first so a clear reject short-circuits
before paying for the VLM round-trip:

  1. striping / barcode artifact check (``_has_striping_artifact``)
     Pure-CV (numpy + a tiny stdlib PNG decoder). VLMs do not reliably see
     pixel-level striping, so this is a separate detector. Threshold needs
     Stage 2 calibration (see TODO).

  2. VLM semantic + text check
     Asks the (already-deployed, multimodal) gemma4 whether the image is an
     abstract, text-free illustration of the concept. The VLM is an INJECTED
     object exposing ``async check(png_bytes, *, concept) -> dict`` so this
     module stays decoupled from the CSP DB/auth/proxy plumbing that lives
     in api/studio.py.

CLIP was de-scoped (design 2026-05-21): clip-vit is NOT deployed and the
stub was always pass-through. ``GeneratedImage.clip_score`` is kept None
for audit-schema stability (spec 3.5) — do NOT remove that field.

The gate mutates each candidate's ``vlm_verdict`` / ``accepted`` audit
fields in place (the GeneratedImage dataclass reserved them in Stage 1)
so the hydration layer can record them in ``image_gen_meta`` regardless
of accept/reject.
"""
from __future__ import annotations

import logging
import struct
import zlib
from typing import Protocol

import numpy as np

from app.services.flux_image_provider import GeneratedImage

logger = logging.getLogger(__name__)


# ── Calibration constants (spec 5.5) ──────────────────────────────────────
# FFT high-frequency energy threshold for the striping/barcode detector,
# measured on the flattest (lowest-variance) region of the image. A clean
# smooth background has near-zero high-frequency energy; striping/barcode
# artifacts inject a strong high-frequency band even in an otherwise flat
# region.
#
# Calibrated 2026-05-21 on a 50-image FLUX set (scripts/calibrate_striping.py):
# good images measured median=0.574 / p95=0.861 flat-region HF energy, so the
# previous 0.06 was ~14x too low and false-rejected the vast majority of clean
# images. Set to good-p95 (0.861): keeps ~95% of good images. The metric did
# NOT separate the labelled-bad set (those failed for non-striping reasons), so
# this is a "don't kill good images" floor — the VLM/CLIP gates remain the
# semantic backstop for the failure modes striping_energy cannot see.
HF_ENERGY_THRESH: float = 0.861

# Fraction of the image (by 16x16 block variance) treated as "flat region".
FLAT_REGION_FRAC: float = 0.3


# ── Injected-dependency contracts ─────────────────────────────────────────
class Vlm(Protocol):
    """Multimodal model wrapper (gemma4 in production).

    ``check`` returns a dict with at least ``match`` (bool) and ``has_text``
    (bool); ``reason`` (str) is optional.
    """

    async def check(self, png_bytes: bytes, *, concept: str) -> dict: ...


# ── The gate ───────────────────────────────────────────────────────────────
async def gate_candidates(
    candidates: list[GeneratedImage],
    *,
    concept_en: str,
    vlm: Vlm,
) -> GeneratedImage | None:
    """Return the best accepted candidate, or ``None`` if all fail.

    Each candidate runs striping -> VLM, cheapest-first: a pixel-level
    striping artifact short-circuits before the VLM round-trip. Accepted
    candidates are ranked by the VLM's 0-1 quality ``score`` and the highest
    wins. CLIP was de-scoped (see spec 2026-05-21 design); ``clip_score`` is
    left None for audit-schema stability.
    """
    scored: list[GeneratedImage] = []
    for c in candidates:
        # Reset audit state — a retried/reused candidate must not carry a
        # stale verdict.
        c.accepted = False

        # Gate 1: striping / barcode artifact (pure CV, cheapest — first).
        if _has_striping_artifact(c.png_bytes):
            logger.info("Gate reject (striping artifact detected)")
            continue

        # Gate 2: VLM semantic + text check (also yields the 0-1 rank score).
        try:
            verdict = await vlm.check(c.png_bytes, concept=concept_en)
        except Exception as e:  # noqa: BLE001
            logger.warning("VLM check raised, skipping candidate: %s", e)
            continue
        c.vlm_verdict = verdict
        if not verdict.get("match") or verdict.get("has_text"):
            logger.info("Gate reject (VLM): %s", verdict)
            continue

        c.accepted = True
        scored.append(c)
        logger.info("[gate] candidate accepted: vlm=%s", c.vlm_verdict)

    if not scored:
        logger.info(
            "[gate] all %d candidate(s) rejected for concept=%r",
            len(candidates), concept_en[:50],
        )
        return None
    best = max(scored, key=lambda x: float((x.vlm_verdict or {}).get("score", 0.0)))
    logger.info(
        "[gate] %d/%d accepted; best vlm score=%.2f",
        len(scored), len(candidates),
        float((best.vlm_verdict or {}).get("score", 0.0)),
    )
    return best


# ── Striping / barcode detector (pure CV) ──────────────────────────────────
def striping_energy(
    png_bytes: bytes,
    *,
    flat_region_frac: float = FLAT_REGION_FRAC,
) -> float | None:
    """Return the median high-freq energy of the flattest region, or ``None``
    if the image cannot be decoded.

    This is the raw metric the striping gate thresholds on; exposed (public)
    so calibration tooling can inspect the *distribution* of the metric over
    a labelled image set, not just the gate's pass/fail boolean. The gate
    (``_has_striping_artifact``) is just this value compared to a threshold.

    Strategy (spec 5.1): a clean diffusion image has a smooth background;
    striping/barcode artifacts inject a strong high-frequency band even
    where the picture is otherwise flat. So:
      1. decode to a grayscale array,
      2. split into 16x16 blocks, take the lowest-variance ``flat_region_frac``
         of them (the smooth background),
      3. for each, measure normalized high-frequency FFT energy,
      4. return the median flat-region HF energy.

    Returns ``None`` (rather than 0.0) when the image cannot be decoded or is
    too small, so callers can distinguish "measured a smooth image" from
    "could not measure". The gate treats ``None`` as fail-open (not striping).
    """
    gray = _decode_png_to_gray(png_bytes)
    if gray is None or gray.size == 0:
        return None

    h, w = gray.shape
    block = 16
    if h < block or w < block:
        return None

    nby, nbx = h // block, w // block
    blocks = []
    variances = []
    for by in range(nby):
        for bx in range(nbx):
            blk = gray[by * block : (by + 1) * block, bx * block : (bx + 1) * block]
            blocks.append(blk)
            variances.append(float(np.var(blk)))

    if not blocks:
        return None

    variances_arr = np.asarray(variances)
    n_flat = max(1, int(len(blocks) * flat_region_frac))
    flat_idx = np.argsort(variances_arr)[:n_flat]

    energies = [_hf_energy(blocks[i]) for i in flat_idx]
    if not energies:
        return None
    return float(np.median(energies))


def _has_striping_artifact(
    png_bytes: bytes,
    *,
    flat_region_frac: float = FLAT_REGION_FRAC,
    hf_energy_thresh: float = HF_ENERGY_THRESH,
) -> bool:
    """Detect barcode/striping artifacts in the flattest region of an image.

    Thin wrapper over ``striping_energy``: flag when the measured median
    flat-region HF energy exceeds ``hf_energy_thresh``.

    Fail-open: if the image cannot be decoded (unsupported PNG variant,
    truncated bytes, too small) ``striping_energy`` returns ``None`` and we
    return ``False`` — the striping gate must never be the reason a perfectly
    good image is dropped on a decode quirk; the VLM and CLIP gates still
    apply.
    """
    e = striping_energy(png_bytes, flat_region_frac=flat_region_frac)
    return e is not None and e > hf_energy_thresh


def _hf_energy(block: np.ndarray) -> float:
    """Normalized high-frequency energy of a 2D block via 2D FFT.

    Returns the fraction of spectral energy living in the outer (high
    frequency) band of the shifted spectrum. A flat patch concentrates
    energy at DC (center) -> near 0; striping puts a strong peak off-center
    -> elevated value.
    """
    arr = block.astype(np.float64)
    arr = arr - arr.mean()  # drop DC so a bright-but-flat patch reads ~0
    if not np.any(arr):
        return 0.0
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(arr))) ** 2
    total = spectrum.sum()
    if total <= 0:
        return 0.0

    bh, bw = spectrum.shape
    cy, cx = bh // 2, bw // 2
    # Low-frequency core = central quarter of each axis.
    ly0, ly1 = cy - bh // 4, cy + bh // 4 + 1
    lx0, lx1 = cx - bw // 4, cx + bw // 4 + 1
    low = spectrum[ly0:ly1, lx0:lx1].sum()
    high = total - low
    return float(high / total)


# ── Minimal stdlib PNG decoder ─────────────────────────────────────────────
# No image library (PIL/cv2/imageio) is installed in the backend venv, only
# numpy. FLUX outputs are standard 8-bit non-interlaced PNGs, so we decode
# that common case directly. Anything outside it returns None and the
# striping gate fails open.
def _decode_png_to_gray(png_bytes: bytes) -> np.ndarray | None:
    try:
        return _decode_png_to_gray_impl(png_bytes)
    except Exception as e:  # noqa: BLE001
        logger.debug("PNG decode failed (striping gate fails open): %s", e)
        return None


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# color_type -> channels per pixel
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _decode_png_to_gray_impl(png_bytes: bytes) -> np.ndarray | None:
    if not png_bytes.startswith(_PNG_SIGNATURE):
        return None

    pos = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    idat = bytearray()

    while pos + 8 <= len(png_bytes):
        (length,) = struct.unpack(">I", png_bytes[pos : pos + 4])
        ctype = png_bytes[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + length
        chunk = png_bytes[data_start:data_end]

        if ctype == b"IHDR":
            (width, height, bit_depth, color_type, _comp, _filt, interlace) = (
                struct.unpack(">IIBBBBB", chunk[:13])
            )
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break

        pos = data_end + 4  # skip the 4-byte CRC

    if width is None or not idat:
        return None
    # Only the common diffusion-output case: 8-bit, non-interlaced.
    if bit_depth != 8 or interlace != 0 or color_type not in _CHANNELS:
        return None

    channels = _CHANNELS[color_type]
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) < expected:
        return None

    out = np.zeros((height, width * channels), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int32)
    bpp = channels  # bytes per pixel at 8-bit depth
    src = 0
    for y in range(height):
        filt = raw[src]
        src += 1
        line = np.frombuffer(raw[src : src + stride], dtype=np.uint8).astype(np.int32)
        src += stride
        recon = _unfilter_scanline(filt, line, prev, bpp)
        out[y] = recon.astype(np.uint8)
        prev = recon

    img = out.reshape(height, width, channels)
    return _to_gray(img, color_type)


def _unfilter_scanline(
    filt: int, line: np.ndarray, prev: np.ndarray, bpp: int
) -> np.ndarray:
    """Reverse one PNG scanline filter (None/Sub/Up/Average/Paeth).

    Sub/Average/Paeth depend on the already-reconstructed pixel ``bpp``
    bytes to the left, so they are computed left-to-right.
    """
    n = line.shape[0]
    recon = np.zeros(n, dtype=np.int32)

    if filt == 0:  # None
        recon[:] = line
    elif filt == 2:  # Up
        recon[:] = (line + prev) & 0xFF
    else:
        for i in range(n):
            a = recon[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            x = line[i]
            if filt == 1:  # Sub
                recon[i] = (x + a) & 0xFF
            elif filt == 3:  # Average
                recon[i] = (x + ((a + b) >> 1)) & 0xFF
            elif filt == 4:  # Paeth
                recon[i] = (x + _paeth(a, b, c)) & 0xFF
            else:
                raise ValueError(f"Unsupported PNG filter {filt}")
    return recon


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _to_gray(img: np.ndarray, color_type: int) -> np.ndarray:
    """Collapse channels to a single luma plane (float not needed here)."""
    if color_type == 0:  # grayscale
        return img[:, :, 0].astype(np.float64)
    if color_type == 4:  # grayscale + alpha
        return img[:, :, 0].astype(np.float64)
    # RGB / RGBA / (palette already excluded) -> Rec.601 luma
    r = img[:, :, 0].astype(np.float64)
    g = img[:, :, 1].astype(np.float64)
    b = img[:, :, 2].astype(np.float64)
    return 0.299 * r + 0.587 * g + 0.114 * b
