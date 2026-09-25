"""簡報／視覺模型名稱不再從環境變數來。

常數是角色哨兵。不管 ANILA_STUDIO_*_MODEL 寫了什麼，都不會變成上游模型名。
"""
from __future__ import annotations

import importlib
import sys

import pytest

from app.services.studio_model_primary import SLIDES_ROLE_SENTINEL, VISION_ROLE_SENTINEL


def _reload(monkeypatch, env: dict[str, str | None]):
    for key in ("ANILA_STUDIO_SLIDES_MODEL", "ANILA_STUDIO_VISION_MODEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import app.services.studio_config as studio_config

    return importlib.reload(studio_config)


@pytest.fixture(autouse=True)
def _restore():
    yield
    if "app.services.studio_config" in sys.modules:
        importlib.reload(sys.modules["app.services.studio_config"])


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"ANILA_STUDIO_SLIDES_MODEL": "", "ANILA_STUDIO_VISION_MODEL": ""},
        {"ANILA_STUDIO_SLIDES_MODEL": "   ", "ANILA_STUDIO_VISION_MODEL": "\t\n"},
        {
            "ANILA_STUDIO_SLIDES_MODEL": "  gemma26  ",
            "ANILA_STUDIO_VISION_MODEL": " gemma26-vl ",
        },
    ],
)
def test_env_model_names_are_ignored(monkeypatch, env):
    studio_config = _reload(monkeypatch, env)
    assert studio_config.SLIDES_LLM_MODEL == SLIDES_ROLE_SENTINEL
    assert studio_config.VISION_LLM_MODEL == VISION_ROLE_SENTINEL
    assert "gemma" not in studio_config.SLIDES_LLM_MODEL
    assert "gemma" not in studio_config.VISION_LLM_MODEL
