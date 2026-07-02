#!/usr/bin/env python3
"""Generate a FLUX calibration image set for striping-threshold tuning (spec 5.5).

Stage 2's striping gate thresholds ``HF_ENERGY_THRESH`` on the median
high-frequency energy of the flattest region of a candidate image. The
shipped value (0.06) was *estimated* and on real runs it false-rejects
legitimately detailed FLUX heroes (e.g. an aircraft cover image, high real
detail -> high FFT HF energy -> wrongly flagged as striping). Spec 5.5 calls
for a 50-image hand-labelled calibration set to pick a threshold that does
NOT kill good images.

This script ONLY generates the image set by calling the flux2-dev
``/generate`` JSON contract. It does no measurement and no labelling — that's
``calibrate_striping.py``. Run it yourself against the live FLUX service:

    python scripts/gen_flux_calibration_set.py --n 50 --out-dir /tmp/flux-calib

Files are named ``calib_<idx>_seed<seed>_<prompt-slug>.png`` so a human can
map each image back to its prompt while labelling good/bad.

Pure stdlib + httpx (the backend venv ships httpx). No project imports, so it
runs standalone from anywhere.
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

import httpx

# House-style suffix (mirrors app.services.flux_style._DEFAULT_STYLE so the
# calibration images look like real production output). FLUX is
# guidance-distilled and ignores negative prompts, so "no text" rides inside
# the positive body.
HOUSE_STYLE_SUFFIX = (
    "flat editorial illustration, isometric perspective, "
    "muted teal and warm gray palette, soft diffused lighting, "
    "generous negative space, clean unmarked surfaces, "
    "no text, no letters, no symbols, no signage"
)

# A diverse base prompt set spanning the themes a deck's cover hero / section
# art might cover: business, tech, nature, abstract, people/scenes, industry.
# The aircraft case (the one that triggered the false-reject) is included on
# purpose so the calibration set contains the known high-detail offender.
BASE_PROMPTS: list[str] = [
    # commercial / business
    "a commercial jet airliner climbing above the clouds",
    "a busy modern office with people collaborating at desks",
    "a handshake closing a business deal in a boardroom",
    "a financial growth chart rising over a city skyline",
    "a shipping port with cranes loading container ships",
    # technology
    "a server room with rows of glowing data racks",
    "a robotic arm assembling components on a factory line",
    "a smartphone surrounded by floating app interface panels",
    "an electric vehicle charging at a roadside station",
    "a satellite orbiting earth beaming data to the ground",
    # nature / environment
    "a lush green forest with sunlight filtering through trees",
    "a wind farm of turbines on rolling coastal hills",
    "a mountain lake reflecting snow-capped peaks at dawn",
    "a field of solar panels under a clear blue sky",
    "ocean waves breaking against a rocky shoreline",
    # abstract / conceptual
    "an abstract network of interconnected glowing nodes",
    "a flowing abstract composition of overlapping shapes",
    "concentric ripples spreading across a calm surface",
    "an abstract representation of data flowing as light streams",
    "a minimalist geometric pattern of layered triangles",
    # people / scenes
    "a teacher presenting to attentive students in a classroom",
    "a doctor reviewing medical scans on a screen",
    "a chef plating a dish in a professional kitchen",
    "a researcher examining samples in a laboratory",
    "a family gathered around a table sharing a meal",
]


def _slug(text: str, max_len: int = 24) -> str:
    """Short filesystem-safe slug of a prompt for the filename."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "prompt"


def _build_jobs(n: int, prompts: list[str], base_seed: int) -> list[tuple[str, int]]:
    """Pair prompts with seeds to produce exactly ``n`` (prompt, seed) jobs.

    Cycles through ``prompts`` and bumps the seed each full pass so we get
    seed diversity (different FLUX rolls) on the same prompt rather than
    duplicate images.
    """
    jobs: list[tuple[str, int]] = []
    if not prompts:
        return jobs
    for i in range(n):
        prompt = prompts[i % len(prompts)]
        seed = base_seed + (i // len(prompts))
        jobs.append((prompt, seed))
    return jobs


def _load_prompts_file(path: Path) -> list[str]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _generate_one(
    client: httpx.Client, flux_url: str, prompt: str, aspect: str, seed: int
) -> bytes:
    """Call flux2-dev /generate for a single image; return decoded PNG bytes."""
    body = {
        "prompt": f"{prompt}, {HOUSE_STYLE_SUFFIX}",
        "aspect_ratio": aspect,
        "seed": seed,
        "num_candidates": 1,
    }
    resp = client.post(f"{flux_url.rstrip('/')}/generate", json=body)
    resp.raise_for_status()
    data = resp.json()
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise RuntimeError(f"flux2-dev returned no images for prompt={prompt!r}")
    return base64.b64decode(images[0])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=50, help="number of images (default 50)")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/tmp/flux-calib"),
        help="output directory (default /tmp/flux-calib)",
    )
    ap.add_argument(
        "--flux-url",
        default="http://flux2-dev:8000",
        help="flux2-dev base URL (default http://flux2-dev:8000)",
    )
    ap.add_argument(
        "--aspect",
        default="16:9",
        help="aspect ratio; cover-hero use case is 16:9 (default)",
    )
    ap.add_argument(
        "--base-seed", type=int, default=1000, help="starting seed (default 1000)"
    )
    ap.add_argument(
        "--prompts-file",
        type=Path,
        default=None,
        help="optional file, one prompt per line, overrides the built-in set",
    )
    ap.add_argument(
        "--timeout", type=float, default=180.0, help="per-request timeout seconds"
    )
    args = ap.parse_args(argv)

    prompts = (
        _load_prompts_file(args.prompts_file)
        if args.prompts_file is not None
        else BASE_PROMPTS
    )
    if not prompts:
        print("ERROR: no prompts to generate from.", file=sys.stderr)
        return 2

    jobs = _build_jobs(args.n, prompts, args.base_seed)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"Generating {len(jobs)} image(s) -> {args.out_dir} "
        f"via {args.flux_url} (aspect {args.aspect})"
    )

    ok = 0
    fail = 0
    with httpx.Client(timeout=args.timeout) as client:
        for idx, (prompt, seed) in enumerate(jobs):
            out_path = args.out_dir / f"calib_{idx:02d}_seed{seed}_{_slug(prompt)}.png"
            try:
                png = _generate_one(client, args.flux_url, prompt, args.aspect, seed)
                out_path.write_bytes(png)
                ok += 1
                print(f"  [{idx + 1}/{len(jobs)}] OK   {out_path.name}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(
                    f"  [{idx + 1}/{len(jobs)}] FAIL {out_path.name}: {e}",
                    file=sys.stderr,
                )

    print(f"Done: {ok} ok, {fail} failed. Images in {args.out_dir}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
