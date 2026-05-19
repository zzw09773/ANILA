"""LaTeX strip — Round 3 Patch I."""
from __future__ import annotations

import pytest

from app.services.studio_text_normalizer import strip_latex


def test_strip_latex_known_arrows():
    assert strip_latex("Observation $\\rightarrow$ Thought") == "Observation → Thought"
    assert strip_latex("A $\\to$ B $\\to$ C") == "A → B → C"
    assert strip_latex("$\\Rightarrow$ implies") == "⇒ implies"


def test_strip_latex_math_ops():
    assert strip_latex("速度 $\\times$ 2") == "速度 × 2"


def test_strip_latex_unknown_command_falls_back():
    # Unknown command → generic regex strips dollar wrappers, keeps inner.
    assert strip_latex("a $\\someweird$ b") == "a \\someweird b"


def test_strip_latex_no_latex_passes_through():
    assert strip_latex("純中文沒有 LaTeX") == "純中文沒有 LaTeX"
    assert strip_latex("") == ""
    assert strip_latex(None) is None


def test_strip_latex_v3_slide12_regression():
    """Exact text from v3 slide 12 that triggered this patch."""
    input_text = "記錄 Observation $\\rightarrow$ Thought $\\rightarrow$ Action 軌跡"
    expected = "記錄 Observation → Thought → Action 軌跡"
    assert strip_latex(input_text) == expected


def test_strip_latex_greek_letters():
    assert strip_latex("$\\alpha$ + $\\beta$") == "α + β"
