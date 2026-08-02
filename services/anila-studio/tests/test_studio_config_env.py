"""Studio LLM 模型常數的 env strip-or-default 契約。

``SLIDES_LLM_MODEL``／``VISION_LLM_MODEL`` 在 import 時讀 env；空字串與
純空白須回落 ``gemma4``，與 ``resolve_model`` 的 strip-or-default 一致。
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _reload_studio_config(monkeypatch, env: dict[str, str | None]):
    """套用 env（None＝刪除）後 reload 模組，回傳新常數值。"""
    for key in ("ANILA_STUDIO_SLIDES_MODEL", "ANILA_STUDIO_VISION_MODEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    import app.services.studio_config as studio_config

    studio_config = importlib.reload(studio_config)
    return studio_config.SLIDES_LLM_MODEL, studio_config.VISION_LLM_MODEL


@pytest.fixture(autouse=True)
def _restore_studio_config_after_env_tests():
    """monkeypatch 還原 env 後，再 reload 一次以免汙染後續測試檔。

    刻意不依賴 ``monkeypatch``：autouse teardown 須排在 monkeypatch
    還原之後，才會在乾淨環境下 reload。
    """
    yield
    if "app.services.studio_config" in sys.modules:
        importlib.reload(sys.modules["app.services.studio_config"])


@pytest.mark.parametrize(
    "env,expected_slides,expected_vision",
    [
        ({}, "gemma4", "gemma4"),
        (
            {
                "ANILA_STUDIO_SLIDES_MODEL": "",
                "ANILA_STUDIO_VISION_MODEL": "",
            },
            "gemma4",
            "gemma4",
        ),
        (
            {
                "ANILA_STUDIO_SLIDES_MODEL": "   ",
                "ANILA_STUDIO_VISION_MODEL": "\t\n",
            },
            "gemma4",
            "gemma4",
        ),
        (
            {
                "ANILA_STUDIO_SLIDES_MODEL": "  gemma26  ",
                "ANILA_STUDIO_VISION_MODEL": " gemma26-vl ",
            },
            "gemma26",
            "gemma26-vl",
        ),
    ],
    ids=["unset", "empty", "whitespace", "padded"],
)
def test_studio_model_env_strip_or_default(
    monkeypatch, env, expected_slides, expected_vision
):
    slides, vision = _reload_studio_config(monkeypatch, env)
    assert slides == expected_slides
    assert vision == expected_vision


def test_studio_model_env_independent_per_key(monkeypatch):
    """一鍵空白、一鍵有值時各自獨立回落／採用。"""
    slides, vision = _reload_studio_config(
        monkeypatch,
        {
            "ANILA_STUDIO_SLIDES_MODEL": "",
            "ANILA_STUDIO_VISION_MODEL": "  vision-x  ",
        },
    )
    assert slides == "gemma4"
    assert vision == "vision-x"


def test_studio_config_reload_cleanup_leaves_pristine_defaults():
    """autouse finalizer 後，乾淨 env 下模組常數必須回到 gemma4。"""
    import app.services.studio_config as studio_config

    assert studio_config.SLIDES_LLM_MODEL == "gemma4"
    assert studio_config.VISION_LLM_MODEL == "gemma4"
