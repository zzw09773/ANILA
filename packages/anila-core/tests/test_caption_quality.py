"""Content — not length — decides repetitive vs truncated."""
from __future__ import annotations

from anila_core.providers.caption_quality import (
    classify_caption,
    is_repetitive_caption,
    mark_truncated,
)


def test_latex_loop_is_repetitive_regardless_of_length():
    loop = "文字內容 (OCR)：** " + "$\\text{}" * 80
    assert is_repetitive_caption(loop) is True
    assert classify_caption(loop, hit_token_limit=True) == "repetitive"


def test_coherent_long_text_is_not_repetitive():
    body = (
        "圖片內容如下：左上角有一個圓形圖示，內有文字 4/26 Sun。"
        "中間是賽道示意圖，右側有海拔曲線。"
    ) * 12
    assert len(body) > 400
    assert is_repetitive_caption(body) is False
    assert classify_caption(body, hit_token_limit=True) == "truncated"
    assert classify_caption(body, hit_token_limit=False) == "ok"


def test_truncated_mark_is_visible_and_idempotent():
    marked = mark_truncated("At the bottom of the image, there is a")
    assert "截斷" in marked
    assert mark_truncated(marked) == marked


def test_honest_unreadable_is_ok():
    text = "這張圖片非常模糊，無法辨識其中的任何文字、圖表、符號或具體內容。"
    assert classify_caption(text) == "ok"
    assert is_repetitive_caption(text) is False
