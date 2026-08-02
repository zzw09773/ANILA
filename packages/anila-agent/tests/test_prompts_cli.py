"""prompts/builder + output_styles + slash_commands。"""

from __future__ import annotations

import pytest

from anila_agent.cli.output_styles import list_output_styles, load_output_style
from anila_agent.cli.slash_commands import load_commands, parse_slash
from anila_agent.prompts.builder import build_instructions

pytestmark = pytest.mark.unit


# ---- builder ----

def test_build_instructions_includes_sections():
    out = build_instructions(system="SYS", memory_index="- mem-a — desc", output_style="STYLE")
    assert "SYS" in out
    assert "輸出風格" in out and "STYLE" in out
    assert "長期記憶索引" in out and "mem-a" in out


def test_build_instructions_minimal():
    out = build_instructions(system="SYS")
    assert "輸出風格" not in out and "長期記憶索引" not in out


def test_build_instructions_preamble_first_by_default():
    from anila_core.prompts import COMMON_PREAMBLE

    out = build_instructions(system="SYS")
    assert out.startswith(COMMON_PREAMBLE), "共同前導必須是靜態前綴（prefix cache）"
    assert out.index(COMMON_PREAMBLE) < out.index("SYS")


def test_build_instructions_preamble_can_be_disabled():
    out = build_instructions(system="SYS", preamble=None)
    assert "ANILA" not in out.split("SYS")[0]  # 前導關閉時 SYS 前面沒有前導內容
    assert out == "SYS"


# ---- output styles ----

def test_builtin_styles_listed():
    styles = list_output_styles()
    assert "zh-tw-formal" in styles and "concise-cited" in styles


def test_load_builtin_style():
    assert "繁體中文" in (load_output_style("zh-tw-formal") or "")
    assert load_output_style("does-not-exist") is None
    assert load_output_style("") is None


def test_user_style_overrides_builtin(tmp_path):
    (tmp_path / "output_styles").mkdir()
    (tmp_path / "output_styles" / "zh-tw-formal.md").write_text("自訂風格", encoding="utf-8")
    assert load_output_style("zh-tw-formal", tmp_path) == "自訂風格"


# ---- slash commands ----

def test_parse_slash():
    assert parse_slash("/help") == ("help", "")
    assert parse_slash("/summarize 主題 X") == ("summarize", "主題 X")
    assert parse_slash("not a command") is None
    assert parse_slash("/") is None


def test_load_and_expand_commands(tmp_path):
    cmd_dir = tmp_path / "commands"
    cmd_dir.mkdir()
    (cmd_dir / "echo.md").write_text(
        "---\ndescription: 回聲\n---\n請複述：{{args}}", encoding="utf-8"
    )
    commands = load_commands(tmp_path)
    assert "echo" in commands
    assert commands["echo"].description == "回聲"
    assert commands["echo"].expand("哈囉") == "請複述：哈囉"
