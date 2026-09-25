"""Release files for the MLSteam lab image. No network, no anila-core install."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _locked(path: Path) -> dict[str, set[str]]:
    logical = path.read_text(encoding="utf-8").replace("\\\n", " ")
    found: dict[str, set[str]] = {}
    for line in logical.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        header = stripped.split(" --hash", 1)[0]
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._+-]*)\s*==\s*([^\s;,]+)", header)
        assert match, stripped
        hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", stripped))
        assert hashes, stripped
        found[match.group(1).lower()] = hashes
    return found


def test_runtime_lock_is_a_hashed_manylinux_closure():
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "未驗證" not in text
    assert "x86_64-manylinux_2_28" in text
    assert "--only-binary :all:" in text
    locked = _locked(ROOT / "requirements.lock")
    direct = [
        line.split("#", 1)[0].strip().lower()
        for line in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert direct == [
        "fastapi",
        "uvicorn",
        "httpx",
        "cryptography",
        "pydantic",
        "starlette",
    ]
    assert set(direct) <= set(locked)
    assert "jupyterlab" not in locked
    assert "jupyterlab" not in direct
    assert len(locked) >= len(direct)


def test_jupyter_lock_is_separate_from_the_runtime_lock():
    jupyter_in = (ROOT / "mlsteam" / "requirements-jupyter.in").read_text(encoding="utf-8")
    assert "jupyterlab" in jupyter_in.lower()
    runtime_names = [
        line.split("#", 1)[0].strip().lower()
        for line in (ROOT / "requirements.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert "jupyterlab" not in runtime_names
    locked = _locked(ROOT / "mlsteam" / "requirements-jupyter.lock")
    assert "jupyterlab" in locked
    text = (ROOT / "mlsteam" / "requirements-jupyter.lock").read_text(encoding="utf-8")
    assert "--only-binary :all:" in text
    assert "x86_64-manylinux_2_28" in text


def test_image_version_and_run_sh_are_present():
    version = (ROOT / "IMAGE_VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", version)
    script = (ROOT / "run.sh").read_text(encoding="utf-8")
    for word in ("start", "stop", "restart", "status", "logs", "compatible_lab_image_version"):
        assert word in script
    dockerfile = (ROOT / "mlsteam" / "Dockerfile").read_text(encoding="utf-8")
    assert "\nFROM python:3.13-slim@sha256:" in f"\n{dockerfile}"
    assert "--network=none" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
