#!/usr/bin/env python3
"""Audit one ABI wheelhouse and optionally write a per-service manifest."""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path


ManifestEntry = tuple[str, str, int]


def canonical_name(name: str) -> str:
    """Return the PEP 503 spelling used by the freeze manifest."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _metadata_identity(wheel: Path) -> tuple[str, str] | None:
    """Read Name/Version when a real wheel carries valid dist-info metadata."""

    try:
        if not zipfile.is_zipfile(wheel):
            return None
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if not metadata_names:
                return None
            text = archive.read(metadata_names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None

    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"Name", "Version"}:
            fields[key] = value.strip()
    if "Name" not in fields or "Version" not in fields:
        return None
    return canonical_name(fields["Name"]), fields["Version"]


def _filename_identity(wheel: Path) -> tuple[str, str]:
    """Parse the PEP 427 wheel filename without a third-party dependency."""

    stem = wheel.name.removesuffix(".whl")
    parts = stem.split("-")
    if len(parts) < 5:
        raise ValueError("wheel filename has fewer than five PEP 427 fields")

    prefix = "-".join(parts[:-3])
    prefix_parts = prefix.rsplit("-", 2)
    version_like = r"\d+(?:\.\d+)*(?:[A-Za-z][0-9A-Za-z_.]*)?"
    if (
        len(prefix_parts) == 3
        and re.fullmatch(version_like, prefix_parts[1])
        and re.fullmatch(r"\d[0-9A-Za-z_.]*", prefix_parts[2])
    ):
        distribution, version = prefix_parts[0], prefix_parts[1]
    else:
        try:
            distribution, version = prefix.rsplit("-", 1)
        except ValueError as exc:
            raise ValueError("wheel filename has no distribution/version separator") from exc

    if not distribution or not version:
        raise ValueError("wheel filename has an empty distribution or version")
    return canonical_name(distribution), version


def wheel_identity(wheel: Path) -> tuple[str, str]:
    return _metadata_identity(wheel) or _filename_identity(wheel)


def _relative(path: Path, house: Path) -> str:
    try:
        return str(path.relative_to(house))
    except ValueError:
        return str(path)


def _discover_manifests(house: Path) -> list[Path]:
    """Find per-service freeze manifests below an ABI house."""

    return sorted(path for path in house.rglob("*.freeze.txt") if path.is_file())


def _read_manifest(path: Path) -> tuple[list[ManifestEntry], list[str]]:
    """Read canonical name==version entries and report malformed lines."""

    entries: list[ManifestEntry] = []
    errors: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"cannot read {path}: {exc}"]

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            errors.append(f"{path}:{line_number}: expected name==version")
            continue
        raw_name, raw_version = line.split("==", 1)
        name = canonical_name(raw_name.strip())
        version = raw_version.strip()
        if not name or not version or any(character.isspace() for character in version):
            errors.append(f"{path}:{line_number}: expected non-empty name==version")
            continue
        entries.append((name, version, line_number))
    return entries, errors


def _write_manifest(
    manifest: Path,
    versions: dict[str, dict[str, list[Path]]],
) -> tuple[bool, str | None]:
    """Write a manifest from the wheels collected in one service stage."""

    duplicate_distributions = {
        name: sorted(version_map) for name, version_map in versions.items() if len(version_map) > 1
    }
    if duplicate_distributions:
        print(
            "FAIL: one service resolve produced multiple versions for a distribution:",
            file=sys.stderr,
        )
        for name in sorted(duplicate_distributions):
            print(
                f"  {name}: {', '.join(duplicate_distributions[name])}",
                file=sys.stderr,
            )
        return False, None

    lines = [
        f"{name}=={version}"
        for name in sorted(versions)
        for version in sorted(versions[name])
    ]
    try:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    except OSError as exc:
        print(f"FAIL: cannot write freeze manifest {manifest}: {exc}", file=sys.stderr)
        return False, None
    return True, f"wrote manifest={manifest}"


def audit_house(house: Path, write_manifest: Path | None = None) -> int:
    if not house.is_dir():
        print(f"FAIL: wheelhouse is not a directory: {house}", file=sys.stderr)
        return 1

    tarballs = sorted(path for path in house.rglob("*.tar.gz") if path.is_file())
    wheels = sorted(path for path in house.rglob("*.whl") if path.is_file())
    versions: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    parse_errors: list[tuple[Path, str]] = []

    for wheel in wheels:
        try:
            name, version = wheel_identity(wheel)
        except ValueError as exc:
            parse_errors.append((wheel, str(exc)))
            continue
        versions[name][version].append(wheel)

    failures = False
    if not wheels:
        failures = True
        print(f"FAIL: wheelhouse contains no wheels (*.whl): {house}", file=sys.stderr)

    if tarballs:
        failures = True
        print("FAIL: source archives remain (*.tar.gz):", file=sys.stderr)
        for tarball in tarballs:
            print(f"  {_relative(tarball, house)}", file=sys.stderr)

    if parse_errors:
        failures = True
        print("FAIL: wheel filenames could not be parsed:", file=sys.stderr)
        for wheel, reason in parse_errors:
            print(f"  {_relative(wheel, house)}: {reason}", file=sys.stderr)

    if write_manifest is not None:
        if failures:
            return 1
        written, note = _write_manifest(write_manifest, versions)
        if not written:
            return 1
        print(f"PASS: {house} ({len(wheels)} wheels, 0 tar.gz, {note})")
        return 0

    manifests = _discover_manifests(house)
    if not manifests:
        failures = True
        print("FAIL: wheelhouse contains no freeze manifests (*.freeze.txt):", file=sys.stderr)
        print(f"  {house}", file=sys.stderr)

    manifest_entries: dict[Path, list[ManifestEntry]] = {}
    manifest_errors: list[str] = []
    duplicate_manifest_distributions: list[tuple[Path, str, list[int]]] = []
    referenced: set[tuple[str, str]] = set()

    for manifest in manifests:
        entries, errors = _read_manifest(manifest)
        manifest_entries[manifest] = entries
        manifest_errors.extend(errors)
        by_name: dict[str, list[ManifestEntry]] = defaultdict(list)
        for entry in entries:
            by_name[entry[0]].append(entry)
            referenced.add((entry[0], entry[1]))
        for name, name_entries in by_name.items():
            if len(name_entries) > 1:
                duplicate_manifest_distributions.append(
                    (manifest, name, [entry[2] for entry in name_entries])
                )

    if manifest_errors:
        failures = True
        print("FAIL: freeze manifest syntax/read errors:", file=sys.stderr)
        for error in manifest_errors:
            print(f"  {error}", file=sys.stderr)

    if duplicate_manifest_distributions:
        failures = True
        print("FAIL: a distribution appears more than once in one freeze manifest:", file=sys.stderr)
        for manifest, name, line_numbers in duplicate_manifest_distributions:
            lines = ", ".join(str(line_number) for line_number in line_numbers)
            print(f"  {_relative(manifest, house)}: {name} (lines {lines})", file=sys.stderr)

    available_pairs = {
        (name, version)
        for name, version_map in versions.items()
        for version in version_map
    }
    missing_entries = sorted(
        (manifest, name, version, line_number)
        for manifest, entries in manifest_entries.items()
        for name, version, line_number in entries
        if (name, version) not in available_pairs
    )
    if missing_entries:
        failures = True
        print("FAIL: freeze manifest entries have no matching wheel:", file=sys.stderr)
        for manifest, name, version, line_number in missing_entries:
            print(
                f"  {_relative(manifest, house)}:{line_number}: {name}=={version}",
                file=sys.stderr,
            )

    orphan_wheels = sorted(
        wheel
        for name, version_map in versions.items()
        for version, version_wheels in version_map.items()
        if (name, version) not in referenced
        for wheel in version_wheels
    )
    if orphan_wheels:
        failures = True
        print("FAIL: wheels are not referenced by any freeze manifest:", file=sys.stderr)
        for wheel in orphan_wheels:
            print(f"  {_relative(wheel, house)}", file=sys.stderr)

    if failures:
        return 1

    print(f"PASS: {house} ({len(wheels)} wheels, 0 tar.gz, {len(manifests)} manifests)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "house",
        type=Path,
        help="ABI wheelhouse directory; normal audit auto-discovers *.freeze.txt below it",
    )
    parser.add_argument(
        "--write-manifest",
        type=Path,
        help="write a sorted manifest from the wheels collected in this staging directory",
    )
    args = parser.parse_args()
    return audit_house(args.house, args.write_manifest)


if __name__ == "__main__":
    raise SystemExit(main())
