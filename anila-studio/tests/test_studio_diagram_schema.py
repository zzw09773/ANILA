"""Slide.image_kind + diagram_dot — schema-level mutual exclusion.

Studio Fix 2 (2026-05-18): FLUX.2-dev is a diffusion model and can't
render legible text in images. Split image_focus into:

  illustration (FLUX)     — atmospheric / concept art, no text
  diagram      (Graphviz) — labelled architecture / flow / ER diagrams

The schema enforces mutual exclusion when image_kind is explicitly set;
legacy paths without image_kind remain valid for backwards compatibility.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import Slide


def test_diagram_kind_with_dot_ok() -> None:
    s = Slide(
        title="X",
        bullets=["a", "b"],
        image_kind="diagram",
        diagram_dot="digraph G { A -> B }",
    )
    assert s.image_kind == "diagram"
    assert s.diagram_dot is not None


def test_illustration_kind_with_prompt_ok() -> None:
    s = Slide(
        title="X",
        bullets=["a", "b"],
        image_kind="illustration",
        image_prompt="A military tank in the mountains",
    )
    assert s.image_kind == "illustration"
    assert s.image_prompt is not None


def test_diagram_kind_with_prompt_rejected() -> None:
    with pytest.raises(ValidationError):
        Slide(
            title="X",
            bullets=["a", "b"],
            image_kind="diagram",
            image_prompt="should be diagram_dot instead",
        )


def test_illustration_kind_with_dot_rejected() -> None:
    with pytest.raises(ValidationError):
        Slide(
            title="X",
            bullets=["a", "b"],
            image_kind="illustration",
            diagram_dot="digraph G { A -> B }",
        )


def test_diagram_kind_missing_dot_rejected() -> None:
    with pytest.raises(ValidationError):
        Slide(
            title="X",
            bullets=["a", "b"],
            image_kind="diagram",
        )


def test_diagram_dot_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        Slide(
            title="X",
            bullets=["a", "b"],
            image_kind="diagram",
            diagram_dot="x" * 3001,
        )
