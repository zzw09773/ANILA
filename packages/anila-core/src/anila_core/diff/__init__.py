"""Programmatic diff of two numbered-article documents."""

from anila_core.diff.article_diff import (
    attach_model_output,
    bind_model_explanations,
    compare_docx_bytes,
    compare_texts,
    explanation_request,
)

__all__ = [
    "attach_model_output",
    "bind_model_explanations",
    "compare_docx_bytes",
    "compare_texts",
    "explanation_request",
]
