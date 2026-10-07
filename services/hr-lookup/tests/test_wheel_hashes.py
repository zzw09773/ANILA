"""輪檔、SHA256SUMS、requirements.txt 的雜湊必須是同一組。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WHEELHOUSE = ROOT / "wheelhouse"
SUMS = WHEELHOUSE / "SHA256SUMS"
REQUIREMENTS = ROOT / "requirements.txt"

EXPECTED = {
    "cffi-2.1.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl":
        "c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf",
    "cryptography-50.0.1-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl":
        "ff838d62ec1bfce4f9ba7fa16f4a7b554cd8d0c299e6be37502161a660c84eef",
    "oracledb-4.0.2-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl":
        "579f2c568433523a990cde5bea73c980d144754dc54d3ab2cd37efd670dc31d6",
    "pycparser-3.0-py3-none-any.whl":
        "b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992",
    "typing_extensions-4.16.0-py3-none-any.whl":
        "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def test_checksum_file_matches_wheels_and_requirements():
    listed = {}
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split()
        listed[name] = digest
    wheels = {path.name for path in WHEELHOUSE.glob("*.whl")}
    assert set(listed) == set(EXPECTED)
    assert wheels == set(EXPECTED)
    for name, digest in EXPECTED.items():
        assert listed[name] == digest
        assert _sha256(WHEELHOUSE / name) == digest

    text = REQUIREMENTS.read_text(encoding="utf-8")
    found = dict(re.findall(r"^([A-Za-z0-9_]+)==([^\s\\]+)", text, re.M))
    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
    assert set(found) == {
        "cffi",
        "cryptography",
        "oracledb",
        "pycparser",
        "typing_extensions",
    }
    assert found["oracledb"] == "4.0.2"
    assert set(hashes) == set(EXPECTED.values())
    assert len(hashes) == len(EXPECTED)
