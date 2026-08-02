"""strip_inline_think 行為表。"""

from anila_core.text.think_strip import strip_inline_think


def test_closed_think_block_removed():
    raw = "前言<think>secret reasoning</think>正文"
    clean, n = strip_inline_think(raw)
    assert clean == "前言正文"
    assert n == len("<think>secret reasoning</think>")
    assert "secret" not in clean


def test_closed_thinking_alias_case_insensitive():
    raw = "A<THINKING>x</THINKING>B<Think>y</think>C"
    clean, n = strip_inline_think(raw)
    assert clean == "ABC"
    assert n > 0
    assert "x" not in clean and "y" not in clean


def test_multiline_non_greedy():
    raw = "開始\n<think>\nline1\nline2\n</think>\n結束\n<think>第二</think>尾"
    clean, n = strip_inline_think(raw)
    assert "line1" not in clean
    assert "第二" not in clean
    assert "開始" in clean and "結束" in clean and "尾" in clean
    assert n > 0


def test_unclosed_tag_at_end():
    raw = "可見內容<think>後面全是推理沒閉合"
    clean, n = strip_inline_think(raw)
    assert clean == "可見內容"
    assert n == len("<think>後面全是推理沒閉合")


def test_unclosed_thinking_alias():
    raw = "ok<thinking attr='x'>dangling"
    clean, n = strip_inline_think(raw)
    assert clean == "ok"
    assert n > 0


def test_nested_ish_repeated_strip():
    # 內層先閉合時，外層殘段再被下一輪／未閉合規則清掉。
    raw = "X<think>outer <think>inner</think> still</think>Y"
    clean, n = strip_inline_think(raw)
    assert "inner" not in clean
    assert "outer" not in clean
    assert "still" not in clean
    assert clean.startswith("X") and clean.endswith("Y")
    assert n > 0


def test_absent_returns_unchanged():
    raw = "完全沒有 think 區塊"
    clean, n = strip_inline_think(raw)
    assert clean == raw
    assert n == 0


def test_empty_and_none_safe():
    assert strip_inline_think("") == ("", 0)
    assert strip_inline_think(None) == ("", 0)
