"""Studio stays one uvicorn process so the in-memory job registry is the registry."""

from pathlib import Path


def test_dockerfile_does_not_add_workers():
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    cmd = [line for line in text.splitlines() if line.startswith("CMD ")]
    assert cmd
    assert "--workers" not in cmd[-1]
