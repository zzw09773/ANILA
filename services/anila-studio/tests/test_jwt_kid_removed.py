"""Studio 驗 JWT 只看 JWKS，不再讀 JWT_KID。"""
from __future__ import annotations

from pathlib import Path


def _service_block(text: str, name: str) -> str:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(f"  {name}:"):
            start = index
            break
    assert start is not None, name
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if (
            line.startswith("  ")
            and not line.startswith("   ")
            and line.strip().endswith(":")
        ):
            break
        block.append(line)
    return "\n".join(block)


def test_settings_model_has_no_jwt_kid():
    from app.config import Settings

    assert "JWT_KID" not in Settings.model_fields


def test_studio_compose_does_not_set_jwt_kid():
    root = Path(__file__).resolve().parents[3]
    for rel in ("infra/compose/platform.yml", "infra/compose/dev.yml"):
        text = (root / rel).read_text(encoding="utf-8")
        block = _service_block(text, "anila-studio")
        assert "JWT_KID" not in block, rel
