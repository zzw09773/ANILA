#!/usr/bin/env python3
"""Striping-threshold calibration tool (spec 5.5).

Two modes:

  measure  — for every PNG in --img-dir, compute the striping HF-energy metric
             via ``app.services.flux_quality_gate.striping_energy`` and write a
             CSV (filename, hf_energy, label) with the ``label`` column left
             BLANK for the human to fill in good/bad.

                 python infra/deployment/scripts/calibrate_striping.py measure \\
                     --img-dir /tmp/flux-calib --out-csv /tmp/calib.csv

             ...then a human opens /tmp/calib.csv and writes ``good`` or
             ``bad`` in the label column of each row (good = a fine FLUX image
             wrongly at risk of being killed; bad = real striping/barcode).

  analyze  — read the now-labelled CSV and report the hf_energy distribution
             for good (and bad, if any) plus a RECOMMENDED HF_ENERGY_THRESH and
             the reasoning.

                 python infra/deployment/scripts/calibrate_striping.py analyze --csv /tmp/calib.csv

The measure mode needs to import the backend service. It auto-locates the
``services/csp`` dir relative to this script and adds it to sys.path,
so it works whether you run it from the repo root or the backend dir. Use the
backend venv python so numpy is available:

    services/csp/.venv/bin/python infra/deployment/scripts/calibrate_striping.py measure ...
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from statistics import median

# Threshold recommendation knobs.
# When BOTH good and bad samples exist we place the threshold midway between
# good-p95 and bad-min (if they're separable). When only good samples exist we
# fall back to good-max scaled by this safety margin.
GOOD_ONLY_MARGIN = 1.2
# Small absolute cushion above good-max even when bad samples bound us, so a
# good image right at the boundary isn't borderline-rejected.
ABS_MARGIN = 0.005


def _backend_on_path() -> None:
    """Add services/csp to sys.path so ``app.services...`` imports."""
    here = Path(__file__).resolve()
    # repo root is infra/deployment/scripts/../../..; backend is services/csp under it.
    candidates = [
        here.parents[3] / "services" / "csp",
        Path.cwd() / "services" / "csp",
        Path.cwd() / "csp",
        Path.cwd(),
    ]
    for c in candidates:
        if (c / "app" / "services" / "flux_quality_gate.py").exists():
            sys.path.insert(0, str(c))
            return
    # Leave path untouched; the import below will raise a clear error.


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (pct in 0..100). Plain stdlib."""
    if not values:
        raise ValueError("empty")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# ── measure mode ─────────────────────────────────────────────────────────────
def cmd_measure(args: argparse.Namespace) -> int:
    _backend_on_path()
    try:
        from app.services.flux_quality_gate import striping_energy
    except Exception as e:  # noqa: BLE001
        print(
            f"ERROR: cannot import app.services.flux_quality_gate.striping_energy: {e}\n"
            "Run with the backend venv python so numpy is importable, e.g.:\n"
            "  services/csp/.venv/bin/python infra/deployment/scripts/calibrate_striping.py "
            "measure --img-dir ... --out-csv ...",
            file=sys.stderr,
        )
        return 2

    img_dir: Path = args.img_dir
    if not img_dir.is_dir():
        print(f"ERROR: --img-dir not a directory: {img_dir}", file=sys.stderr)
        return 2

    pngs = sorted(p for p in img_dir.iterdir() if p.suffix.lower() == ".png")
    if not pngs:
        print(f"ERROR: no .png files in {img_dir}", file=sys.stderr)
        return 2

    rows: list[tuple[str, str, str]] = []
    for p in pngs:
        try:
            energy = striping_energy(p.read_bytes())
        except Exception as e:  # noqa: BLE001
            print(f"  WARN {p.name}: measure failed: {e}", file=sys.stderr)
            energy = None
        energy_str = "" if energy is None else f"{energy:.6f}"
        rows.append((p.name, energy_str, ""))
        print(f"  {p.name}: hf_energy={energy_str or 'None'}")

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["filename", "hf_energy", "label"])
        w.writerows(rows)

    print(
        f"\nWrote {len(rows)} rows to {args.out_csv}\n"
        "Next: open the CSV and fill the 'label' column with 'good' or 'bad' "
        "for each row, then run:\n"
        f"  python {Path(__file__).name} analyze --csv {args.out_csv}"
    )
    return 0


# ── analyze mode ─────────────────────────────────────────────────────────────
def _read_labelled(csv_path: Path) -> tuple[list[float], list[float], int]:
    """Return (good_energies, bad_energies, n_unlabelled_or_unmeasured)."""
    good: list[float] = []
    bad: list[float] = []
    skipped = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = (row.get("hf_energy") or "").strip()
            label = (row.get("label") or "").strip().lower()
            if not raw or label not in ("good", "bad"):
                skipped += 1
                continue
            try:
                val = float(raw)
            except ValueError:
                skipped += 1
                continue
            (good if label == "good" else bad).append(val)
    return good, bad, skipped


def _describe(name: str, values: list[float]) -> None:
    if not values:
        print(f"  {name}: (none)")
        return
    print(
        f"  {name} (n={len(values)}): "
        f"min={min(values):.5f} median={median(values):.5f} "
        f"p95={_percentile(values, 95):.5f} max={max(values):.5f}"
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    csv_path: Path = args.csv
    if not csv_path.is_file():
        print(f"ERROR: --csv not found: {csv_path}", file=sys.stderr)
        return 2

    good, bad, skipped = _read_labelled(csv_path)
    print(f"Labelled samples: good={len(good)} bad={len(bad)} skipped={skipped}\n")
    if not good:
        print(
            "ERROR: no rows labelled 'good' with a numeric hf_energy. "
            "Fill the label column first.",
            file=sys.stderr,
        )
        return 2

    print("Distributions:")
    _describe("good", good)
    _describe("bad ", bad)
    print()

    good_max = max(good)
    good_p95 = _percentile(good, 95)

    if bad:
        bad_min = min(bad)
        if bad_min > good_max:
            # Cleanly separable: sit just above good-max, but no higher than
            # bad-min, biased toward good so we never re-introduce a false kill.
            rec = min(good_max + ABS_MARGIN, (good_max + bad_min) / 2.0)
            reason = (
                f"good and bad are separable (good-max={good_max:.5f} < "
                f"bad-min={bad_min:.5f}); placing threshold just above good-max "
                f"(+{ABS_MARGIN}) so no good image is rejected and real "
                f"striping (>= {bad_min:.5f}) is still caught."
            )
        else:
            # Overlap: no single threshold is perfect. Use good-p95 as the
            # tolerance (accept 95% of good) and warn about the overlap.
            rec = good_p95
            reason = (
                f"good and bad OVERLAP (bad-min={bad_min:.5f} <= "
                f"good-max={good_max:.5f}); the metric cannot perfectly "
                f"separate them. Recommending good-p95={good_p95:.5f}: this "
                f"keeps ~95% of good images. Some bad images below this will "
                f"pass the striping gate (the VLM gate is the backstop). "
                f"Consider revisiting the metric (block size / flat-region "
                f"fraction) if overlap is large."
            )
    else:
        # Only good samples (the likely real-world case — striping is the
        # rare failure we couldn't reliably reproduce).
        rec = good_max * GOOD_ONLY_MARGIN
        reason = (
            f"NO bad samples labelled. Recommending good-max x {GOOD_ONLY_MARGIN} "
            f"= {good_max:.5f} x {GOOD_ONLY_MARGIN} = {rec:.5f}. This guarantees "
            f"zero false-rejects on the calibration set with headroom; it does "
            f"NOT prove the gate catches real striping (no positive samples to "
            f"confirm against). The VLM gate remains the semantic backstop. To "
            f"set a discriminating threshold, add a few hand-made striped/"
            f"barcode samples (label them 'bad') and re-run analyze."
        )

    print("=" * 64)
    print(f"RECOMMENDED HF_ENERGY_THRESH = {rec:.5f}")
    print("  (current shipped value: 0.06)")
    print(f"Reason: {reason}")
    print("=" * 64)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    m = sub.add_parser("measure", help="compute hf_energy per image -> CSV")
    m.add_argument("--img-dir", type=Path, required=True)
    m.add_argument("--out-csv", type=Path, required=True)
    m.set_defaults(func=cmd_measure)

    a = sub.add_parser("analyze", help="read labelled CSV -> recommended threshold")
    a.add_argument("--csv", type=Path, required=True)
    a.set_defaults(func=cmd_analyze)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
